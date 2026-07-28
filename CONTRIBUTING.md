# Contributing to Paper Knowledge Base

Thanks for your interest in contributing! This document explains how to submit issues and pull requests, the repository workflow, and how to run tests locally.

## Where to start
- Check existing issues for similar bugs or feature requests before opening a new one.
- If you're adding a feature or fixing a bug, please open a pull request from a branch named `feature/<short-description>` or `fix/<short-description>`.

## Branch & PR workflow
- Create a new branch from the repository default branch (`master`):

  git checkout master
  git pull
  git checkout -b feature/your-short-description

- Keep PRs focused: one logical change per PR.
- Write a clear PR description: what changed, why, and any migration/compatibility notes.
- Link related issues in the PR description.

## Coding style and tests
- Preferred Python version: 3.10+ (project uses modern typing features).
- Formatting: you may use `black` or any formatter you prefer. We don't enforce it via CI yet, but please keep diffs readable.
- Add or update tests for changes that affect behavior. Tests live under `paper-knowledge-base/tests/`.

## Running tests locally
1. Create and activate a virtual environment:

   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS / Linux
   source .venv/bin/activate

2. Install dependencies:

   pip install -r paper-knowledge-base/requirements.txt

3. Run tests:

   python -m pytest -s

(Use `-s` because some tests rely on stdout behavior on Windows.)

## Reporting bugs
- Provide a short description, steps to reproduce, expected vs actual behavior, and minimal reproduction if possible.
- Include platform details (OS, Python version) and any relevant logs.

## Submitting a PR
1. Fork the repo (if you don't have push access). 2. Create a branch and commit changes. 3. Open a PR with a descriptive title. 4. Fill in the PR description template if available.

Thanks — your contributions are welcome!
