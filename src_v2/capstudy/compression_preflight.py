"""Source inventory and prompt preview only; no network calls or token estimates."""
import argparse
import json
from pathlib import Path
from .bundles import load_bundle, split_front_matter, fence_errors, sha256
from .compress.xing import STRATEGIES, render_prompt
from .compress.sentences import segment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("milestone 1/data"))
    parser.add_argument("--output", type=Path, default=Path("results/study-v1/preflight"))
    args = parser.parse_args(argv)
    skill_root = args.data_root / "shared/skills/human_authored"
    inventory = []
    for root in sorted(p for p in skill_root.iterdir() if p.is_dir()):
        source = load_bundle(root)
        units, headings = segment(source)
        inventory.append({"family": root.name, "instruction_characters_not_tokens": sum(map(len, source.files.values())),
            "skill_files": [{"path": p, "characters_not_tokens": len(t), "utf8_bytes": len(t.encode()),
                "front_matter_bytes": len(split_front_matter(t)[0].encode()),
                "source_wrapper_issues": fence_errors(t)} for p, t in sorted(source.files.items())],
            "instruction_tokens": None, "token_cap": None,
            "count_status": "unavailable: no approved/validated executor counting route",
            "support_files": len(source.support), "sentence_block_units": len(units),
            "ancestor_headings": len(headings), **source.hashes()})
    args.output.mkdir(parents=True, exist_ok=True)
    result = {"evidence_kind": "source_inspection", "model_calls": 0,
              "paid_calls": 0, "family_count": len(inventory),
              "skill_document_count": sum(len(i["skill_files"]) for i in inventory),
              "largest_family_by_characters": max(inventory, key=lambda i: i["instruction_characters_not_tokens"])["family"],
              "families": inventory}
    (args.output / "bundle_inventory.json").write_text(json.dumps(result, indent=2) + "\n")
    gdp = load_bundle(skill_root / "weighted-gdp-calculation")
    for strategy in STRATEGIES:
        text = "SOURCE-ONLY PREVIEW: NOT DISPATCHABLE; COUNTS UNAVAILABLE.\n\n" + render_prompt(
            gdp, strategy, "UNAVAILABLE_PROVIDER_COUNT", "UNAVAILABLE_PROVIDER_CAP",
            "unresolved; no character-count substitution permitted")
        (args.output / f"gdp_prompt_preview_{strategy}.txt").write_text(text)
    print(json.dumps({"inventory": str(args.output / "bundle_inventory.json"),
                      "families": len(inventory), "skill_files": result["skill_document_count"],
                      "model_calls": 0, "counts_available": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
