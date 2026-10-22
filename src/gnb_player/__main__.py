"""Dispatch the GUI or its explicit installer subprocess without importing Qt early."""
import sys


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == '--model-install-worker':
        from .model_install_worker import main as worker_main
        return worker_main(arguments[1:])
    from .app import main as app_main
    return app_main(arguments)


if __name__ == '__main__':
    raise SystemExit(main())
