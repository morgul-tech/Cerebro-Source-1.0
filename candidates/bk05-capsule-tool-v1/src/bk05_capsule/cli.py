"""Command line front end.  File I/O lives here only; stdout carries one JSON document, stderr carries errors.

Exit codes: 0 LOCAL_CAPSULE_CANDIDATE / reconstruction succeeded | 10 FULL_TASK_REQUIRED | 11 HOLD_STALE |
12 INVALID_INPUT | 2 usage or refused output | 3 unexpected tool failure.
"""
import argparse
import json
import os
import sys

from . import core, synthetic

EXIT = {core.LOCAL_CAPSULE_CANDIDATE: 0, core.RECONSTRUCTED: 0, core.FULL_TASK_REQUIRED: 10, core.HOLD_STALE: 11,
        core.INVALID_INPUT: 12}


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Usage(message)


def _read(path):
    if path is None:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None  # absent or unreadable: reported as an absent input by the core


def _dump(obj):
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_all(out_dir, files):
    """Create each file exclusively (never overwrite).  Refuses before writing anything if a name already exists."""
    os.makedirs(out_dir, exist_ok=True)
    for name in files:
        if os.path.lexists(os.path.join(out_dir, name)):
            raise _Usage("OUTPUT_EXISTS: refusing to overwrite %s" % os.path.join(out_dir, name))
    for name, data in files.items():
        with open(os.path.join(out_dir, name), "xb") as fh:
            fh.write(data)


def _parser():
    p = _Parser(prog="python -m bk05_capsule", description="BK05 continuation capsule (local, structural-only)")
    sub = p.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="validate inputs, decide, and (if LOCAL_CAPSULE_CANDIDATE) build capsule + measurement")
    for flag in ("parent", "parent-manifest", "verifier", "verifier-delta", "currentness", "full-continuation",
                 "prior-record", "out-dir"):
        b.add_argument("--" + flag)
    b.add_argument("--reuse-assumption", choices=core.REUSE_ASSUMPTIONS, default="REREAD_ALL")
    r = sub.add_parser("reconstruct", help="verify hashes and emit exact parent/verifier bytes plus typed capsule fields")
    for flag in ("capsule", "parent", "verifier"):
        r.add_argument("--" + flag)
    r.add_argument("--out-dir", required=True)
    s = sub.add_parser("synthetic-fixture", help="write the self-created synthetic positive-case inputs")
    s.add_argument("--out-dir", required=True)
    return p


def _emit(stream, obj):
    stream.write(_dump(obj))
    stream.flush()


def _err(stream, exit_code, code, message):
    _emit(stream, {"schema_version": "bk05-error-v1", "exit_code": exit_code, "error_code": code, "message": message})


def main(argv=None, stdout=None, stderr=None):
    out = stdout if stdout is not None else sys.stdout.buffer
    err = stderr if stderr is not None else sys.stderr.buffer
    try:
        if sys.version_info < (3, 12):
            raise RuntimeError("Python 3.12 or newer is required")
        args = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
        if args.cmd is None:
            raise _Usage("a command is required: build | reconstruct | synthetic-fixture")
        if args.cmd == "synthetic-fixture":
            _write_all(args.out_dir, synthetic.positive_fixture())
            _emit(out, {"schema_version": "bk05-fixture-v1", "synthetic": True, "files": sorted(synthetic.positive_fixture())})
            return 0
        if args.cmd == "build":
            full, prior = _read(args.full_continuation), _read(args.prior_record)
            # An explicitly named optional file that cannot be read is an absent input, never a silent skip.
            named_absent = [n for flag, n, raw in ((args.full_continuation, "full_continuation.txt", full),
                                                   (args.prior_record, "prior_record.json", prior))
                            if flag is not None and raw is None]
            ev = core.evaluate(_read(args.parent), _read(args.parent_manifest), _read(args.verifier),
                               _read(args.verifier_delta), _read(args.currentness), full, prior,
                               args.reuse_assumption, named_absent)
            doc = ev.decision_document()
            if args.out_dir:
                files = {"decision.json": _dump(doc)}
                if ev.capsule:
                    files["capsule.json"] = core.capsule_file_bytes(ev.capsule)
                    files["measurement.json"] = core.canonical_bytes(ev.measurement) + b"\n"
                _write_all(args.out_dir, files)
            _emit(out, doc)
            return EXIT[ev.decision]
        # reconstruct
        decision, reasons, payload = core.reconstruct(_read(args.capsule), _read(args.parent), _read(args.verifier))
        if payload is None:
            _emit(out, {"schema_version": core.SCHEMA_RECONSTRUCTION, "decision": decision, "reasons": reasons,
                        "authority": core.AUTHORITY, "validation": core.VALIDATION_LABEL})
            return EXIT[decision]
        _write_all(args.out_dir, {"parent.txt": _read(args.parent), "verifier.txt": _read(args.verifier),
                                  "reconstruction.json": _dump(payload)})
        _emit(out, payload)
        return 0
    except _Usage as exc:
        _err(err, 2, "USAGE_OR_OUTPUT_REFUSED", str(exc))
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - contract: unexpected tool failure -> exit 3
        _err(err, 3, "TOOL_FAILURE", "%s: %s" % (type(exc).__name__, exc))
        return 3
