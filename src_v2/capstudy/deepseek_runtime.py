"""Staged-only DeepSeek relay mapping. No credential discovery or live execution.

This stdlib-only module is copied into a verified independent harness stage. Its
mapping is part of config.runtime, hence the shared execution signature. The
solver receives a trial relay token, never the provider key held by the gateway.
"""
from __future__ import annotations
import copy
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shlex
import shutil
from urllib.parse import urlsplit

MODEL = "deepseek-flash"
URL_ENV = "CAPSTUDY_DEEPSEEK_RELAY_URL"
TOKEN_ENV = "CAPSTUDY_DEEPSEEK_RELAY_TOKEN"
SOLVER_HOME = "/tmp/capstudy-solver-home"
CONFIG_DIR = SOLVER_HOME + "/.claude"
SKILL_DIRECTORY = CONFIG_DIR + "/skills"
SKILL_LOADING_DIRECTIVE = ("Before working on the task, invoke each installed task-specific skill listed in the available skills. "
    "If no task-specific skills are installed, proceed without one.")
LOCAL_TOOLS = frozenset({"Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS",
    "NotebookEdit", "NotebookRead", "TodoRead", "TodoWrite", "Skill"})
DENIED_TOOLS = ("Agent", "Task", "SlashCommand", "WebFetch", "WebSearch")
ALIASES = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
           "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
FIXED_ENV = {
    "HOME": SOLVER_HOME, "CLAUDE_CONFIG_DIR": CONFIG_DIR,
    "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384", "CLAUDE_CODE_MAX_RETRIES": "0",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1", "DO_NOT_TRACK": "1", "DISABLE_GROWTHBOOK": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CODE_AUTO_CONNECT_IDE": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    "DISABLE_LOGIN_COMMAND": "1", "DISABLE_LOGOUT_COMMAND": "1",
    "DISABLE_DOCTOR_COMMAND": "1",
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def module_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def validate_relay_url(url):
    """Literal private address only: no public proxy, DNS, credentials or query."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("Private relay URL required")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Relay URL must contain only a private origin and optional /anthropic path")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Relay URL requires a literal private IP and valid port") from exc
    private = ((address.version == 4 and any(address in ipaddress.ip_network(n) for n in
        ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))) or
        (address.version == 6 and (address == ipaddress.ip_address("::1") or address in ipaddress.ip_network("fc00::/7"))))
    if not private or port is None or not 1 <= port <= 65535 or parsed.path not in ("", "/anthropic"):
        raise ValueError("Relay must use an explicit port on a private literal IP")
    return url


def make_mapping(*, relay_url, cli_path, cli_version, allowed_tools, max_turns,
                 network_policy_sha256, mock_capture_sha256=None, skill_loading_policy=None):
    """Create a nonsecret descriptor; this does not assert runtime readiness."""
    validate_relay_url(relay_url)
    mapping = {"schema_version": 1, "route": "private-deepseek-anthropic-relay",
        "model_id": MODEL, "relay_url_env": URL_ENV, "relay_token_env": TOKEN_ENV,
        "relay_url_sha256": hashlib.sha256(relay_url.encode()).hexdigest(),
        "cli_path": cli_path, "cli_version": cli_version, "allowed_tools": sorted(allowed_tools),
        "disallowed_tools": list(DENIED_TOOLS), "max_turns": max_turns,
        "max_output_tokens": 16384, "max_retries": 0,
        "solver_home": SOLVER_HOME, "config_directory": CONFIG_DIR, "skill_directory": SKILL_DIRECTORY,
        "model_aliases": {name: MODEL for name in ALIASES}, "fixed_environment": dict(FIXED_ENV),
        "network_policy_sha256": network_policy_sha256, "mock_capture_sha256": mock_capture_sha256,
        "implementation_sha256": module_sha256(), "formal_execution_ready": False}
    if skill_loading_policy is not None:
        mapping["skill_loading_policy"] = skill_loading_policy
        mapping["skill_loading_directive"] = SKILL_LOADING_DIRECTIVE
    validate_mapping(mapping)
    return mapping


def validate_mapping(mapping):
    if mapping.get("skill_loading_policy") is not None:
        if mapping["skill_loading_policy"] != "invoke-installed-before-task-v1" or mapping.get("skill_loading_directive") != SKILL_LOADING_DIRECTIVE:
            raise ValueError("Unreviewed skill-loading policy")
    elif "skill_loading_directive" in mapping:
        raise ValueError("Skill-loading directive requires bound policy")
    if mapping.get("schema_version") != 1 or mapping.get("route") != "private-deepseek-anthropic-relay" or mapping.get("model_id") != MODEL:
        raise ValueError("Wrong DeepSeek mapping protocol/model")
    if mapping.get("implementation_sha256") != module_sha256():
        raise ValueError("Mapping implementation changed; rebuild/review the nonsecret descriptor")
    if not re.fullmatch(r"\d+\.\d+\.\d+", mapping.get("cli_version") or ""):
        raise ValueError("Exact pinned CLI version required")
    path = mapping.get("cli_path")
    if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or ".." in Path(path).parts or not path.endswith("/claude"):
        raise ValueError("Safe absolute pinned Claude CLI path required")
    turns = mapping.get("max_turns")
    if type(turns) is not int or turns < 1:
        raise ValueError("Positive explicit reviewed pilot turn limit required")
    allowed = mapping.get("allowed_tools")
    if not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed) or any(t not in LOCAL_TOOLS for t in allowed):
        raise ValueError("Only the reviewed local tools may be exposed")
    expected = {"relay_url_env": URL_ENV, "relay_token_env": TOKEN_ENV, "max_output_tokens": 16384,
        "max_retries": 0, "solver_home": SOLVER_HOME, "config_directory": CONFIG_DIR,
        "skill_directory": SKILL_DIRECTORY, "disallowed_tools": list(DENIED_TOOLS),
        "model_aliases": {name: MODEL for name in ALIASES}, "fixed_environment": dict(FIXED_ENV),
        "formal_execution_ready": False}
    if any(mapping.get(key) != value for key, value in expected.items()):
        raise ValueError("Fixed alias/credential/isolation policy changed")
    for key in ("relay_url_sha256", "network_policy_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", mapping.get(key) or ""):
            raise ValueError("Relay route and network-policy hashes are required")
    if mapping.get("mock_capture_sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", mapping["mock_capture_sha256"]):
        raise ValueError("Invalid mock capture hash")
    return mapping


def solver_environment(host_environment, mapping):
    """Whitelist mapping from a supplied env dictionary; never read secret files."""
    validate_mapping(mapping)
    relay_url = validate_relay_url(host_environment.get(URL_ENV))
    if hashlib.sha256(relay_url.encode()).hexdigest() != mapping["relay_url_sha256"]:
        raise ValueError("Injected relay route differs from the signature-bound mapping")
    token = host_environment.get(TOKEN_ENV)
    if not isinstance(token, str) or not re.fullmatch(r"capstudy-relay-[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("Dedicated per-trial relay token required; provider keys are prohibited")
    return {**FIXED_ENV, **mapping["model_aliases"], "ANTHROPIC_BASE_URL": relay_url,
            "ANTHROPIC_AUTH_TOKEN": token}


def registry_agent(agent, mapping):
    validate_mapping(mapping)
    result = copy.deepcopy(agent)
    result.update(name="Claude Code via private DeepSeek relay (pilot)",
        env=["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"], default_model=MODEL,
        default_tools=list(mapping["allowed_tools"]), setup=clean_home_setup(), trajectory_env={})
    return result


def clean_home_setup():
    """Copy selected skill bytes only, never original user config or auth state."""
    return ("umask 077; test ! -e " + shlex.quote(SOLVER_HOME) +
        " && mkdir -p " + shlex.quote(SKILL_DIRECTORY) +
        " && if [ -d /root/.claude/skills ]; then cp -a /root/.claude/skills/. " +
        shlex.quote(SKILL_DIRECTORY) + "; fi")


def agent_command(mapping, *, model, max_steps, task_disallowed=()):
    """No provider key or relay-token value is rendered in this shell command."""
    validate_mapping(mapping)
    if model != MODEL or type(max_steps) is not int or max_steps != mapping["max_turns"]:
        raise ValueError("Effective model/turn limit differs from reviewed mapping")
    if any(not isinstance(t, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", t) for t in task_disallowed):
        raise ValueError("Invalid task tool denial")
    allowed = [t for t in mapping["allowed_tools"] if t not in set(task_disallowed)]
    denied = sorted(set(DENIED_TOOLS) | set(task_disallowed))
    settings = {"disableAllHooks": True, "disableClaudeAiConnectors": True,
        "disableWorkflows": True, "disableBundledSkills": True, "autoMemoryEnabled": False, "autoCompactEnabled": False,
        "alwaysThinkingEnabled": False,
        "enabledPlugins": {"cc-plugin-plugin-authoring@builtin": False, "cc-plugin-agents-md@builtin": False},
        "disableSkillShellExecution": True, "permissions": {"deny": denied}}
    argv = [mapping["cli_path"], "--verbose", "--output-format", "stream-json", "--model", MODEL,
        "--max-turns", str(max_steps), "--tools", ",".join(allowed),
        "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(denied),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "user",
        "--add-dir", SKILL_DIRECTORY,
        "--settings", json.dumps(settings, sort_keys=True, separators=(",", ":")),
        "--permission-mode", "dontAsk", "--permission-prompts", "none", "--no-chrome",
        "--no-session-persistence", "-p"]
    # env -i discards image-baked API keys, OAuth/proxy variables and hooks. Values
    # admitted by solver_environment are already inside the isolated container.
    names = sorted(set(FIXED_ENV) | set(ALIASES) | {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"})
    prefix = "env -i " + " ".join(name + '=\"${' + name + ':?missing mapped environment}\"' for name in names)
    plain = prefix + " " + shlex.join(argv) + ' "$(cat /tmp/instruction.txt)"'
    if not mapping.get("skill_loading_policy"):
        return plain
    # Section 2: skill-bearing conditions share one loading instruction;
    # no-skill receives no such instruction. Only the selected isolated directory
    # controls this branch, not a family name, task outcome or provider response.
    argv[-1:-1] = ["--append-system-prompt", mapping["skill_loading_directive"]]
    with_skill = prefix + " " + shlex.join(argv) + ' "$(cat /tmp/instruction.txt)"'
    return ('if [ -n "$(find ' + shlex.quote(SKILL_DIRECTORY) +
        ' -type f -name SKILL.md -print -quit)" ]; then ' + with_skill + '; else ' + plain + '; fi')


def _replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Staged harness differs from pinned mapping target")
    return text.replace(old, new, 1)


def apply_staged_mapping(stage, config):
    """Patch only independently staged source; reject immutable/history trees."""
    stage = Path(stage).resolve()
    source_root = Path(__file__).resolve().parents[2] / "milestone 1"
    if stage == source_root or source_root in stage.parents or not (stage / "PROJECT_SPLIT.json").is_file():
        raise ValueError("A separate PROJECT_SPLIT-marked stage is required")
    if any((stage / p).is_symlink() or not (stage / p).resolve().is_relative_to(stage)
           or (stage / p).stat().st_nlink != 1
           for p in ("core/eval_runner.py", "agents/__init__.py", "PROJECT_SPLIT.json")):
        raise ValueError("Symlinked stage inputs prohibited")
    mapping = validate_mapping(config["runtime"]["deepseek_mapping"])
    if config["executor"].get("provider") != "deepseek" or config["executor"].get("agent_id") != "claude-code":
        raise ValueError("Mapping applies only to the explicit DeepSeek Claude-Code pilot")
    if config["executor"].get("model_id") != MODEL or config["executor"].get("cli_version") != mapping["cli_version"] or config["executor"].get("max_steps") != mapping["max_turns"]:
        raise ValueError("Executor configuration disagrees with mapping")
    if sorted(config["tool_policy"].get("allowed_tools", [])) != mapping["allowed_tools"] or not set(DENIED_TOOLS).issubset(config["tool_policy"].get("disallowed_tools", [])):
        raise ValueError("Tool-policy configuration disagrees with mapping")
    path = stage / "core/eval_runner.py"
    text = path.read_text()
    text = _replace_once(text, "import json_repair\n", "import json_repair\nfrom capstudy_deepseek_runtime import solver_environment, registry_agent, agent_command\n" +
        "_DEEPSEEK_MAPPING = " + repr(mapping) + "\n")
    text = _replace_once(text, '    # Check required env vars\n    for env_var in agent["env"]:\n        if not os.environ.get(env_var):\n            msg = f"Set {env_var} (or add to SkillLearnBench/.env) to run agent {agent_id}"\n            print(msg, file=sys.stderr)\n            return 2, {"task_id": task_id, "error": msg}\n',
        '    if agent_id != "claude-code":\n        raise ValueError("This stage only permits the reviewed relay executor")\n' +
        '    agent = registry_agent(agent, _DEEPSEEK_MAPPING)\n    _relay_environment = solver_environment(os.environ, _DEEPSEEK_MAPPING)\n')
    text = _replace_once(text, '    task_cfg = _load_task_config(task_path)\n',
        '    task_cfg = _load_task_config(task_path)\n    if task_cfg.get("environment", {}).get("required_env", []):\n        raise ValueError("Task-required credentials need separate review; GDP pilot expects none")\n')
    text = _replace_once(text, '        env_args = []\n        for env_var in agent["env"]:\n            val = os.environ.get(env_var)\n            if val:\n                env_args.extend(["-e", f"{env_var}={val}"])\n',
        '        env_args = []\n        for env_var, val in sorted(_relay_environment.items()):\n            env_args.extend(["-e", f"{env_var}={val}"])\n')
    old_command = ('            run_cmd = (\n                agent["run"]\n'
        '                .replace("{instruction_file}", "/tmp/instruction.txt")\n'
        '                .replace("{model}", model_name)\n'
        '                .replace("{max_steps}", str(max_steps))\n'
        '                .replace("{allowed_tools}", _allowed_tools)\n'
        '                .replace("{extra_flags}", _extra_flags)\n            )')
    text = _replace_once(text, old_command,
        '            run_cmd = agent_command(_DEEPSEEK_MAPPING, model=model_name, max_steps=max_steps, task_disallowed=_disallowed)')
    compile(text, str(path), "exec")
    if (stage / "capstudy_deepseek_runtime.py").exists():
        raise ValueError("DeepSeek mapping already installed; use a new stage")
    shutil.copyfile(__file__, stage / "capstudy_deepseek_runtime.py")
    path.write_text(text)
    (stage / "DEEPSEEK_MAPPING.json").write_text(json.dumps({"mapping": mapping, "mapping_sha256": _hash(mapping),
        "solver_key_exposed": False, "live_readiness_established": False}, indent=2) + "\n")
    return {"mapping_sha256": _hash(mapping), "implementation_sha256": mapping["implementation_sha256"],
            "live_readiness_established": False}
