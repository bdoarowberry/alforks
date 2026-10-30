from .cli import main

# Guarded so worker processes (spawn on Windows) can import this module safely.
if __name__ == "__main__":
    raise SystemExit(main())
