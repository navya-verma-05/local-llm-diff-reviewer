from pathlib import Path


def format_csv_row(values: list[str]) -> str:
    return ",".join(values)
