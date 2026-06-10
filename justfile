set shell := ["bash", "-uc"]
set unstable
set quiet

# Show available commands
default:
    @just --list

# Run the dashboard
build:
    uv run src/build.py
