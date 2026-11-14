#!/usr/bin/env python3
"""Split topgrade into parallel lanes: one `topgrade --yes <args>` per lane, from ~/.config/upall/tools-lanes.conf.

Prints one line per lane, tab-separated: NAME<TAB>topgrade-arg<TAB>topgrade-arg... Prints nothing when there is
nothing to split (no config, empty config, or a problem: then the reason is on stderr) and the caller runs plain
`topgrade --yes`, one step after another, exactly as before lanes existed.

Config, one lane per line:   NAME: step, step, cmd:custom command name
  step              a topgrade step name (`topgrade --help` lists them), e.g. pipx, uv, claude_code
  cmd:NAME          a custom command from topgrade.toml [commands], e.g. cmd:npm globals
Everything not listed runs in a last lane called "rest", so no step is dropped when topgrade gains new ones.

Safety net: every lane (and the plan as a whole) is dry-run with `topgrade -n`, and the lanes must cover the full
plan exactly once. `--only` ignores the `disable` list in topgrade.toml, so a lane naming a disabled step, a typo,
a step in two lanes or a command that fell between lanes would otherwise run too much, too little or twice.

Usage: tools_lanes.py [--selftest]      (UPALL_TOOLS_LANES=FILE overrides the config path)
"""
import os, re, subprocess, sys
from collections import Counter

CONF = os.environ.get("UPALL_TOOLS_LANES") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "upall", "tools-lanes.conf")
ENV_C = {**os.environ, "LC_ALL": "C.UTF-8"}
STEP_HDR = re.compile(r"―― \d\d:\d\d:\d\d - (.+?) ――")
LANE_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
STEP_NAME = re.compile(r"[a-z0-9_]+\Z")


def parse(text):
    """-> [(lane, [steps], [custom commands])]; raises ValueError naming the line that is wrong."""
    lanes, seen = [], set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, items = line.partition(":")
        name = name.strip()
        if not sep or not LANE_NAME.match(name):
            raise ValueError(f"line {n}: expected 'NAME: step, step, cmd:command' (NAME: letters, digits, - _)")
        if name == "rest" or name in seen:
            raise ValueError(f"line {n}: lane name '{name}' is " + ("reserved (the lane for everything else)"
                                                                      if name == "rest" else "used twice"))
        seen.add(name)
        steps, cmds = [], []
        for it in (i.strip() for i in items.split(",")):
            if it.startswith("cmd:"):
                c = it[4:].strip()
                if not c or "\t" in c or "\x00" in c:
                    raise ValueError(f"line {n}: empty or invalid custom command name")
                cmds.append(c)
            elif STEP_NAME.match(it):
                steps.append(it)
            elif it:
                raise ValueError(f"line {n}: '{it}' is not a topgrade step name (lowercase, digits, _) or cmd:NAME")
        if not steps and not cmds:
            raise ValueError(f"line {n}: lane '{name}' has no steps")
        lanes.append((name, steps, cmds))
    return lanes


def lane_args(steps, cmds):
    args = ["--only", *steps]
    if cmds:
        args += ["custom_commands", "--custom-commands", *cmds]
    return args


def build(lanes, custom_all):
    """[(lane, topgrade args)] with the final 'rest' lane that runs everything the others don't."""
    out = [(name, lane_args(steps, cmds)) for name, steps, cmds in lanes]
    steps_all = [s for _, steps, _ in lanes for s in steps]
    cmds_all = {c for _, _, cmds in lanes for c in cmds}
    rest = ["--disable", *steps_all] if steps_all else []
    if cmds_all:
        left = [c for c in custom_all if c not in cmds_all]
        if left:
            rest += ["--custom-commands", *left]   # the unclaimed custom commands still run, in 'rest'
        elif "custom_commands" not in steps_all:
            rest += (["custom_commands"] if steps_all else ["--disable", "custom_commands"])
    out.append(("rest", rest))
    return out


def dry(args):
    r = subprocess.run(["topgrade", "-n", "--no-self-update", *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, env=ENV_C, timeout=120)
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", r.stdout)
    return [s for s in STEP_HDR.findall(text) if s != "Summary"]


def plan(text, dry=dry):
    """[(lane, args)] to run in parallel; raises ValueError when the lanes don't cover the plan exactly once."""
    lanes = parse(text)
    if not lanes:
        return []
    full = dry([])
    lane_args_ = build(lanes, dry(["--only", "custom_commands"]))
    got = Counter()
    for name, args in lane_args_:
        steps = dry(args)
        if not steps and name != "rest":
            raise ValueError(f"lane '{name}' runs nothing: check its step names (topgrade --help)")
        got.update(steps)
    want = Counter(full)
    problems = []
    if want - got:
        problems.append("not run by any lane: " + ", ".join(sorted((want - got).elements())))
    if got - want:
        problems.append("run although topgrade's own plan skips them (disabled in topgrade.toml?) or twice: "
                        + ", ".join(sorted((got - want).elements())))
    if problems:
        raise ValueError("; ".join(problems))
    return lane_args_


def main():
    if "--selftest" in sys.argv:
        return selftest()
    try:
        text = open(CONF).read()
    except FileNotFoundError:
        return 0
    except OSError as e:
        print(f"tools lanes: cannot read {CONF}: {e}", file=sys.stderr)
        return 0
    try:
        lanes = plan(text)
    except (ValueError, subprocess.SubprocessError, OSError) as e:
        print(f"tools lanes ignored ({e}); running topgrade one step after another. Config: {CONF}", file=sys.stderr)
        return 0
    for name, args in lanes:
        print("\t".join([name, *args]))
    return 0


def selftest():
    P = "\n# c\nnpm: cmd:npm globals\ned: cmd:VS Code, claude_code, codex   # trailing\n"
    lanes = parse(P)
    assert lanes == [("npm", [], ["npm globals"]), ("ed", ["claude_code", "codex"], ["VS Code"])], lanes
    assert build(lanes, ["npm globals", "VS Code"]) == [
        ("npm", ["--only", "custom_commands", "--custom-commands", "npm globals"]),
        ("ed", ["--only", "claude_code", "codex", "custom_commands", "--custom-commands", "VS Code"]),
        ("rest", ["--disable", "claude_code", "codex", "custom_commands"])]
    assert build(lanes, ["npm globals", "VS Code", "mine"])[-1] == (
        "rest", ["--disable", "claude_code", "codex", "--custom-commands", "mine"])
    assert build([("a", ["pipx"], [])], [])[-1] == ("rest", ["--disable", "pipx"])
    assert build([("a", [], ["x"])], ["x"])[-1] == ("rest", ["--disable", "custom_commands"])
    for bad in ("x", "a b: pipx", "rest: pipx", "a: pipx\na: uv", "a: Pipx", "a:", "a: cmd:"):
        try:
            parse(bad); raise SystemExit(f"selftest: accepted {bad!r}")
        except ValueError:
            pass

    # plan(): a fake topgrade whose steps are a, b, c plus custom commands c1, c2.
    def fake(args):
        a = list(args)
        if a == []:
            return ["a", "b", "c", "c1", "c2"]
        if a == ["--only", "custom_commands"]:
            return ["c1", "c2"]
        only = a[a.index("--only") + 1:] if "--only" in a else None
        out = []
        if only is not None:
            names, cmds = [], []
            cur = names
            for x in only:
                if x == "--custom-commands":
                    cur = cmds
                elif x.startswith("--"):
                    cur = names
                else:
                    cur.append(x)
            out += [x for x in names if x in ("a", "b", "c")]
            if "custom_commands" in names:
                out += cmds or ["c1", "c2"]
            return out
        off = a[a.index("--disable") + 1:] if "--disable" in a else []
        off = [x for x in off if not x.startswith("--")]
        out = [x for x in ("a", "b", "c") if x not in off]
        if "--custom-commands" in a:
            out += [x for x in a[a.index("--custom-commands") + 1:] if x in ("c1", "c2")]
        elif "custom_commands" not in off:
            out += ["c1", "c2"]
        return out
    ok = plan("l1: a, cmd:c1\n", fake)
    assert [n for n, _ in ok] == ["l1", "rest"], ok
    for bad, why in (("l1: a\nl2: a\n", "twice"), ("l1: zzz\n", "runs nothing")):
        try:
            plan(bad, fake); raise SystemExit(f"selftest: accepted {bad!r}")
        except ValueError as e:
            assert why in str(e), (bad, e)
    assert plan("", fake) == []
    print("selftest: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
