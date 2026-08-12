"""Guards against a name being used without being imported.

This exists because 1.1.0 shipped `run.py` using USER_AGENT without importing
it. `python -m py_compile` passed (a missing import is a runtime NameError,
not a syntax error) and no test exercised `main()`, so the break only surfaced
when the container started on a real Home Assistant box.

The check disassembles every function and verifies that each name it will look
up as a global actually resolves in that module -- which is exactly what a
missing import breaks, without having to execute the function.
"""
import builtins
import dis
import importlib
import os
import sys
import types

# tests/ and tools/ sit beside the add-on directory, so put it on the path.
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

# Every module shipped in the container (must match the Dockerfile COPY line).
MODULES = ["version", "events", "logic", "radar", "forecast", "shade",
           "run", "irradiance", "solar"]


def _functions(obj, seen=None):
    """Functions DEFINED in this module -- not ones merely imported into it.

    The __module__ filter matters: `solar.py` does `from astral import
    Observer`, and without it the walk descends into astral's own code and
    flags its internals as unresolved names in our module.
    """
    seen = seen if seen is not None else set()
    own = getattr(obj, "__name__", None)
    out = []

    def mine(f):
        return getattr(f, "__module__", None) == own

    for value in vars(obj).values():
        if isinstance(value, types.FunctionType) and id(value) not in seen:
            seen.add(id(value))
            if mine(value):
                out.append(value)
        elif isinstance(value, type) and id(value) not in seen:
            seen.add(id(value))
            if getattr(value, "__module__", None) == own:
                out.extend(v for v in vars(value).values()
                           if isinstance(v, types.FunctionType) and mine(v))
    return out


def _global_names(func):
    """Names the function will look up as a global at runtime, including
    inside comprehensions and nested functions (separate code objects)."""
    names, stack = set(), [func.__code__]
    while stack:
        code = stack.pop()
        for ins in dis.get_instructions(code):
            if ins.opname in ("LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"):
                names.add(ins.argval)
        stack.extend(c for c in code.co_consts
                     if isinstance(c, types.CodeType))
    return names


def _unresolved(module):
    """Global names a module's functions reference but cannot resolve."""
    missing = {}
    for func in _functions(module):
        for name in _global_names(func):
            if name in vars(module) or hasattr(builtins, name):
                continue
            missing.setdefault(name, []).append(func.__name__)
    return missing


def test_every_module_imports_cleanly():
    for name in MODULES:
        importlib.import_module(name)      # raises on a bad import


def test_no_unresolved_global_names():
    problems = []
    for name in MODULES:
        module = importlib.import_module(name)
        for missing, funcs in _unresolved(module).items():
            problems.append(f"{name}: {missing!r} used in {', '.join(funcs)}")
    assert not problems, "unresolved global names:\n  " + "\n  ".join(problems)


def test_dockerfile_copies_every_module():
    """A module that exists but is not COPYed breaks the container at import
    time, not at build time -- so the two lists must agree."""
    root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade"))
    # Join backslash continuations first: a wrapped COPY line would otherwise
    # be invisible to a startswith("COPY") scan, and a module hidden on the
    # second line is exactly what this test exists to catch.
    with open(os.path.join(root, "Dockerfile")) as fh:
        text = fh.read().replace("\\\n", " ")
    copied = {tok for line in text.splitlines() if line.startswith("COPY")
              for tok in line.split() if tok.endswith(".py")}
    on_disk = {f for f in os.listdir(root) if f.endswith(".py")}
    assert on_disk == copied, (
        f"Dockerfile COPY and the add-on directory disagree: "
        f"only on disk={sorted(on_disk - copied)}, "
        f"only in COPY={sorted(copied - on_disk)}")


def test_version_is_consistent_across_manifests():
    """VERSION, config.yaml and the Dockerfile LABEL are bumped by hand and
    have drifted twice; keep them pinned together."""
    import version
    root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade"))
    with open(os.path.join(root, "config.yaml")) as fh:
        cfg = [l for l in fh if l.startswith("version:")][0].split('"')[1]
    with open(os.path.join(root, "Dockerfile")) as fh:
        lbl = [l for l in fh if "io.hass.version" in l][0].split('"')[1]
    assert version.VERSION == cfg == lbl, (
        f"version drift: version.py={version.VERSION}, "
        f"config.yaml={cfg}, Dockerfile={lbl}")


def _module_attr_uses(func, modules):
    """(module_name, attr) pairs the function will look up at runtime.

    Bytecode for `events.set_persist_path(...)` is LOAD_GLOBAL 'events' then
    LOAD_ATTR/LOAD_METHOD 'set_persist_path', so pairing the two finds calls
    into a sibling module.
    """
    pairs, stack = set(), [func.__code__]
    while stack:
        code = stack.pop()
        ins = list(dis.get_instructions(code))
        for a, b in zip(ins, ins[1:]):
            if (a.opname == "LOAD_GLOBAL" and a.argval in modules
                    and b.opname in ("LOAD_ATTR", "LOAD_METHOD")):
                pairs.add((a.argval, b.argval))
        stack.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
    return pairs


def test_no_calls_into_missing_module_attributes():
    """Guards against calling a sibling module function that no longer exists.

    `test_no_unresolved_global_names` cannot see this: `events` resolves fine
    as a global, and the missing name is an ATTRIBUTE on it. A leftover
    `events.set_persist_path(...)` in run.py survived a refactor this way and
    would have crashed the container at startup.
    """
    loaded = {name: importlib.import_module(name) for name in MODULES}
    problems = []
    for name, module in loaded.items():
        for func in _functions(module):
            for mod_name, attr in _module_attr_uses(func, loaded):
                if not hasattr(loaded[mod_name], attr):
                    problems.append(
                        f"{name}.{func.__name__} calls {mod_name}.{attr}, "
                        f"which does not exist")
    assert not problems, "missing module attributes:\n  " + "\n  ".join(problems)


def _section_keys(path, section):
    """Top-level keys of a mapping section, without needing PyYAML."""
    import re
    keys, inside, indent = [], False, None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if re.match(rf"^{section}:\s*$", line):
                inside = True
                continue
            if not inside or not line.strip():
                continue
            if not re.match(r"^\s", line):          # dedent ends the section
                inside = False
                continue
            m = re.match(r"^(\s+)([A-Za-z0-9_]+):", line)
            if m:
                if indent is None:
                    indent = len(m.group(1))
                if len(m.group(1)) == indent:
                    keys.append(m.group(2))
    return keys


def _addon_dir():
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade"))


def test_options_schema_and_translations_agree():
    """A key present in one file but not the others makes the Supervisor
    reject config.yaml -- which shows up as an add-on that silently refuses
    to update, not as an obvious error."""
    root = _addon_dir()
    opts = set(_section_keys(os.path.join(root, "config.yaml"), "options"))
    schema = set(_section_keys(os.path.join(root, "config.yaml"), "schema"))
    trans = set(_section_keys(os.path.join(root, "translations", "en.yaml"),
                              "configuration"))
    assert opts == schema, (
        f"options vs schema: only in options={sorted(opts - schema)}, "
        f"only in schema={sorted(schema - opts)}")
    assert schema == trans, (
        f"schema vs translations: only in schema={sorted(schema - trans)}, "
        f"only in translations={sorted(trans - schema)}")


def test_schema_types_are_valid():
    """Only the types the Supervisor documents; an unknown one is rejected."""
    import re
    valid = ("str", "bool", "int", "float", "email", "url", "password",
             "port", "match(", "list(", "device")
    bad = []
    with open(os.path.join(_addon_dir(), "config.yaml"), encoding="utf-8") as fh:
        in_schema = False
        for line in fh:
            if re.match(r"^schema:\s*$", line):
                in_schema = True
                continue
            if in_schema and line.strip() and not re.match(r"^\s", line):
                break
            m = re.match(r"^\s+([A-Za-z0-9_]+):\s*(.+?)\s*$", line) \
                if in_schema else None
            if m and not m.group(2).strip('"\'').rstrip("?").startswith(valid):
                bad.append(f"{m.group(1)}={m.group(2)}")
    assert not bad, f"invalid schema types: {bad}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
