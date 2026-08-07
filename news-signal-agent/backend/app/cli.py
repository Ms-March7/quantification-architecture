from __future__ import annotations

import argparse
from pathlib import Path

from app.config import load_local_env
from app.workflow.demo import run_demo_workflow
from app.workflow.news import run_news_workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="News Signal Agent CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("run-demo", help="Run the end-to-end mock workflow")
    demo.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Project root containing config/ and reports/",
    )
    news = subparsers.add_parser("run-news", help="Run the live news workflow")
    news.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Project root containing config/ and reports/",
    )
    news.add_argument(
        "--allow-mock-fallback",
        action="store_true",
        help="Use deterministic mock news only if all live collectors return no items.",
    )

    args = parser.parse_args()
    load_local_env(args.project_root)
    if args.command == "run-demo":
        result = run_demo_workflow(args.project_root)
        print(f"Run ID: {result['run_id']}")
        print(f"Markdown report: {result['markdown_path']}")
        print(f"HTML report: {result['html_path']}")
    elif args.command == "run-news":
        result = run_news_workflow(args.project_root, allow_mock_fallback=args.allow_mock_fallback)
        print(f"Run ID: {result['run_id']}")
        print(f"News items: {result['news_items']}")
        print(f"Signals: {result['signals']}")
        print(f"Markdown report: {result['markdown_path']}")
        print(f"HTML report: {result['html_path']}")


if __name__ == "__main__":
    main()
