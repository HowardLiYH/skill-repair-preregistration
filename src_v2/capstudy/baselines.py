"""M2 entry points: generation from verified counts and frozen shared evaluation.

Default mode is a cost-free plan. This module never calls an editor or silently
downloads a checkpoint. Recorded responses and cached provider counts are inputs.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from .bundles import load_bundle, save_bundle, sha256, validate_candidate
from .contracts import CANDIDATE_FIELDS, validate_candidate as validate_record
from .tokenization import CountTokensCounter, CountUnavailable, bundle_count
from .compress.truncate import head_truncate
from .compress.xing import STRATEGIES, parse_response, postprocess
from .compress.llmlingua2 import llmlingua_compress, load_released_compressor


def configured_counter(config, model_id, cache_dir, config_dir):
    """Explicit prospective selection; legacy Anthropic cache remains default."""
    if config.get('executor',{}).get('model_id')!=model_id:
        raise ValueError('Count model must equal configured executor model')
    settings=config.get('tokenizer',{})
    if not isinstance(settings,dict):raise ValueError('Tokenizer configuration must be an object')
    kind=settings.get('id','anthropic-count-tokens')
    if kind=='anthropic-count-tokens':
        if settings.get('model_id') not in (None,model_id):raise ValueError('Configured Anthropic count model mismatch')
        if settings.get('convention',CountTokensCounter.convention)!=CountTokensCounter.convention:
            raise ValueError('Unsupported Anthropic counting convention')
        counter=CountTokensCounter(model_id,cache_dir)
        if settings.get('revision') not in (None,counter.revision):raise ValueError('Configured Anthropic count revision mismatch')
        return counter
    if kind!='deepseek-official-local-tokenizer':raise ValueError('Unsupported tokenizer; no fallback is allowed')
    from .deepseek_tokenization import DeepSeekOfflineCounter
    from .compress.validated_cpu import configured_path
    if model_id!='deepseek-flash':raise ValueError('Offline baseline route supports only the explicitly selected DeepSeek Flash executor')
    if settings.get('executor_model_id')!=model_id:
        raise ValueError('Explicit offline tokenizer binding to the executor model is required')
    if settings.get('convention')!=DeepSeekOfflineCounter.convention:
        raise ValueError('Unsupported offline counting convention')
    if settings.get('api_deployment_equivalence')!='not_verified' or settings.get('provider_counted') is not False or settings.get('estimates_not_billing') is not True:
        raise ValueError('Offline counts must preserve unverified API equivalence and nonbilling provenance')
    if not isinstance(settings.get('protocol_decision'),str) or not settings['protocol_decision'].strip():
        raise ValueError('Explicit prospective counting protocol decision is required')
    required=('model_id','model_revision','tokenizer_sha256','runtime_version')
    if any(not isinstance(settings.get(key),str) or not settings[key] for key in required):
        raise ValueError('Pinned offline tokenizer model, revision, artifact and runtime required')
    counter=DeepSeekOfflineCounter(configured_path(settings.get('artifact_path'),config_dir),
        model_id=settings['model_id'],revision=settings['model_revision'],
        expected_sha256=settings['tokenizer_sha256'],expected_runtime_version=settings['runtime_version'],
        cache_dir=cache_dir)
    if settings.get('revision') not in (None,counter.revision):raise ValueError('Offline tokenizer composite revision mismatch')
    return counter


def main(default_method="head_truncation", argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--method", choices=("head_truncation", "unchanged_rewrite", "llmlingua2"), default=default_method)
    parser.add_argument("--strategy", choices=tuple(STRATEGIES))
    parser.add_argument("--response", type=Path)
    parser.add_argument("--model-id")
    parser.add_argument("--count-cache", type=Path)
    parser.add_argument("--campaign-id", default="study-v1")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--llmlingua-checkpoint-local", type=Path)
    parser.add_argument("--checkpoint-revision")
    parser.add_argument("--compressor-window", type=int)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--frozen", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--generate", action="store_true", help="Generate from saved response/counts; no editor call")
    args = parser.parse_args(argv)
    if args.frozen:
        from .evaluate import main as evaluate_main
        forwarded = ["--frozen", str(args.frozen), "--conditions", args.method,
                     "--execute" if args.execute else "--plan"]
        return evaluate_main(forwarded)
    if args.execute:
        parser.error("Task execution requires a frozen manifest; generation uses --generate")
    if not args.source:
        parser.error("--source or --frozen is required")
    source = load_bundle(args.source)
    if not args.generate:
        print(json.dumps({"executed": False, "method": args.method,
            "source": str(source.root), "skill_paths": sorted(source.files),
            "instruction_characters_not_tokens": sum(map(len, source.files.values())),
            "original_tokens": None, "candidate_tokens": None,
            "requires": ["verified exact-model provider count cache", "frozen calibrated protocol before tests"],
            "task_outputs_available": False}, indent=2))
        return 0
    if not all((args.output, args.model_id, args.count_cache, args.config)):
        parser.error("Generation requires --output, --model-id, --count-cache and --config")
    config = json.loads(args.config.read_text())
    created_output = False
    cpu_attempt_started = False
    try:
        counter = configured_counter(config,args.model_id,args.count_cache,args.config.resolve().parent)
        if args.output.exists():raise ValueError('Generation requires a new output directory')
        original = bundle_count(source.files, counter)
        if args.method == "head_truncation":
            result = head_truncate(source, counter)
        elif args.method == "unchanged_rewrite":
            if not args.response or not args.strategy:
                parser.error("Xing generation requires saved --response and frozen --strategy")
            files = parse_response(args.response.read_text(), source)
            processed = postprocess(source, files, args.strategy, counter)
            result = {"method": args.method, **processed,
                      **validate_candidate(source, processed["files"], counter, original // 2)}
        else:
            cpu=config.get('llmlingua_cpu')
            if cpu is not None:
                if any((args.llmlingua_checkpoint_local,args.checkpoint_revision,args.compressor_window)) or args.device!='cpu':
                    raise ValueError('Validated CPU configuration cannot be overridden by legacy compressor flags')
                from .compress.validated_cpu import compress_validated_cpu
                # Create the evidence directory before inference so completed rates
                # survive later failures. No model calls are retried implicitly.
                args.output.mkdir(parents=True,exist_ok=False)
                created_output = True
                def persist(index,attempt):
                    (args.output/('rate-'+format(attempt['rate'],'.2f')+'.json')).write_text(json.dumps(attempt,indent=2)+'\n')
                cpu_attempt_started = True
                result=compress_validated_cpu(source,counter,cpu,args.config.resolve().parent,on_attempt=persist)
            else:
                if counter.tokenizer_id!='anthropic-count-tokens':
                    raise ValueError('New offline-count LLMLingua generation requires the validated CPU configuration')
                if not args.llmlingua_checkpoint_local or not args.llmlingua_checkpoint_local.is_dir() or not args.checkpoint_revision or not args.compressor_window:
                    parser.error("LLMLingua requires an existing local checkpoint, pinned revision and tested window; downloads are disabled")
                compressor = load_released_compressor(str(args.llmlingua_checkpoint_local), args.checkpoint_revision, args.device)
                result = llmlingua_compress(source, counter,
                    lambda text, rate: compressor.compress_prompt(text, rate=rate, force_tokens=["\n"]),
                    lambda text: compressor.tokenizer.encode(text, add_special_tokens=True), args.compressor_window)
        if not args.output.exists():
            args.output.mkdir(parents=True, exist_ok=False)
            created_output = True
        bundle_path = None
        hashes = {}
        if result.get("files") is not None:
            bundle_path = save_bundle(source, result["files"], args.output / "bundle")
            hashes = source.hashes(result["files"])
        candidate = {key: None for key in CANDIDATE_FIELDS}
        candidate.update({"schema_version": 1, "campaign_id": args.campaign_id,
            "candidate_id": args.output.name, "family": source.root.name,
            "method": args.method, "strategy_id": args.strategy, "attempt_index": 0,
            "source_bundle_sha256": source.hashes()["source_bundle_sha256"],
            "bundle_path": str(bundle_path.resolve()) if bundle_path else None,
            "instruction_sha256": hashes.get("instruction_sha256"), "support_sha256": hashes.get("support_sha256"),
            "file_sha256": hashes.get("file_sha256"), "file_modes": hashes.get("file_modes"),
            "original_tokens": result["original_tokens"], "candidate_tokens": result["candidate_tokens"],
            "token_cap": result["token_cap"], "tokenizer_id": counter.tokenizer_id,
            "tokenizer_revision": counter.revision, "eligible": result["eligible"],
            "ineligibility_reasons": result["ineligibility_reasons"], "call_ids": [],
            "response_path": str(args.response.resolve()) if args.response else None,
            "config_sha256": sha256(args.config.read_bytes()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_provenance_status": "imported_response_requires_call_ledger_link" if args.response else "deterministic_local"})
        if hasattr(counter,'provenance'):
            candidate['count_provenance']={**counter.provenance,'configured_executor_model_id':args.model_id,
                'protocol_decision':config['tokenizer']['protocol_decision']}
        if 'compressor_provenance' in result:
            candidate['compressor_provenance']=result['compressor_provenance']
            candidate['generation_provenance_status']='local_cpu_model_compression'
        validate_record(candidate)
        (args.output / "candidate.json").write_text(json.dumps(candidate, indent=2) + "\n")
        (args.output / "compression.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"candidate": str(args.output / "candidate.json"), "eligible": result["eligible"],
                          "task_outputs_available": False}, indent=2))
        return 0
    except (CountUnavailable, ValueError, TimeoutError) as exc:
        if created_output and config.get('llmlingua_cpu') is not None and args.method=='llmlingua2' and not (args.output/'candidate.json').exists():
            (args.output/'failure.json').write_text(json.dumps({'status':'infrastructure_error','type':type(exc).__name__,
                'reason':str(exc),'no_candidate_claimed':True,'automatic_retry':False,
                'model_execution_status':'unknown_or_partial' if cpu_attempt_started else 'not_started'},indent=2)+'\n')
        print(json.dumps({"executed": None if cpu_attempt_started else False,
            "status": "infrastructure_error" if cpu_attempt_started else "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        if created_output and config.get('llmlingua_cpu') is not None and args.method=='llmlingua2':
            (args.output/'failure.json').write_text(json.dumps({'status':'infrastructure_error','type':type(exc).__name__,
                'reason':str(exc),'no_candidate_claimed':True,'automatic_retry':False,
                'model_execution_status':'unknown_or_partial' if cpu_attempt_started else 'not_started'},indent=2)+'\n')
        raise


if __name__ == '__main__':
    raise SystemExit(main())
