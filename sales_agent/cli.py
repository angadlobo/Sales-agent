"""Command-line interface.

Examples:
    # Full campaign from a config file (dry run by default)
    python -m sales_agent.cli run --config config.yaml

    # Quick one-off without a config file
    python -m sales_agent.cli run \\
        --product "Acme CRM" \\
        --description "Simple CRM for small law firms" \\
        --location "Denver" --industry "law firms" --max-leads 10

    # Just find and score leads, no outreach
    python -m sales_agent.cli discover --config config.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys

from rich.console import Console
from rich.table import Table

from . import pipeline
from .config import CampaignConfig, Settings
from .models import Product, Targeting

console = Console()


def _settings_from_args(args) -> Settings:
    if args.config:
        settings = Settings.from_yaml(args.config)
    else:
        if not (args.product and args.description):
            console.print(
                "[red]Provide --config, or both --product and --description.[/red]"
            )
            sys.exit(2)
        settings = Settings(
            product=Product(name=args.product, description=args.description),
            targeting=Targeting(
                location=args.location, industry=args.industry, max_leads=args.max_leads
            ),
            campaign=CampaignConfig(),
        )

    # CLI overrides
    if args.location:
        settings.targeting.location = args.location
    if args.industry:
        settings.targeting.industry = args.industry
    if args.max_leads:
        settings.targeting.max_leads = args.max_leads
    if getattr(args, "channels", None):
        settings.campaign.channels = args.channels.split(",")
    if getattr(args, "send", False):
        settings.dry_run = False
    return settings


def _print_results(qualified) -> None:
    table = Table(title="Qualified leads", show_lines=False)
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Company")
    table.add_column("Contact")
    table.add_column("Email")
    table.add_column("Phone")
    table.add_column("Outreach")

    for ql in qualified:
        outreach = ", ".join(f"{o.channel.value}:{o.status.value}" for o in ql.outreach) or "—"
        score_style = "green" if ql.best_score >= 65 else "yellow" if ql.best_score >= 40 else "dim"
        table.add_row(
            f"[{score_style}]{ql.best_score}[/{score_style}]",
            ql.lead.company_name,
            ql.lead.contact_name or "—",
            ql.lead.email or "—",
            ql.lead.phone or "—",
            outreach,
        )
    console.print(table)


def cmd_run(args) -> None:
    settings = _settings_from_args(args)
    mode = "[red]LIVE — will send/dial[/red]" if not settings.dry_run else "[green]dry run[/green]"
    console.print(f"Running campaign for [bold]{settings.product.name}[/bold] ({mode})")
    qualified = pipeline.run_campaign(settings, suppression_path=args.suppression)
    _print_results(qualified)


def cmd_discover(args) -> None:
    settings = _settings_from_args(args)
    settings.dry_run = True
    settings.campaign.channels = []  # discovery + scoring only
    console.print(f"Discovering leads for [bold]{settings.product.name}[/bold]")
    qualified = pipeline.run_campaign(settings, save=True)
    _print_results(qualified)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sales-agent", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--config", help="Path to config.yaml")
        p.add_argument("--product", help="Product/service name")
        p.add_argument("--description", help="What it does")
        p.add_argument("--location", help="Target location")
        p.add_argument("--industry", help="Target industry")
        p.add_argument("--max-leads", type=int, default=0, help="How many leads to find")
        p.add_argument("--suppression", help="Path to a do-not-contact list file")

    run_p = sub.add_parser("run", help="Full campaign: discover, score, and reach out")
    add_common(run_p)
    run_p.add_argument("--channels", help="Comma-separated: email,voice")
    run_p.add_argument(
        "--send",
        action="store_true",
        help="Actually send/dial. Without this, runs in dry-run mode.",
    )
    run_p.set_defaults(func=cmd_run)

    disc_p = sub.add_parser("discover", help="Find and score leads only (no outreach)")
    add_common(disc_p)
    disc_p.set_defaults(func=cmd_discover)

    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args.func(args)


if __name__ == "__main__":
    main()
