"""Plain-text descriptions shared by the email-asset commands."""
from __future__ import annotations


def human_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown size"
    value = float(size_bytes)
    for unit in ("bytes", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} bytes"


def describe_asset(asset, *, assignment_count: int | None = None) -> list[str]:
    lines = [
        f"Asset:        {asset.key}  ({asset.get_status_display()})",
        f"Name:         {asset.name}",
        f"Attaches as:  {asset.filename}  [{asset.mime_type}, {human_size(asset.size_bytes)}]",
        f"Source:       {asset.get_provider_display()} {asset.source_url}",
        f"File SHA-256: {asset.sha256}",
    ]
    if asset.approved_sha256:
        state = "current" if asset.approved_sha256 == asset.sha256 else "SUPERSEDED"
        lines.append(f"Approved:     {asset.approved_sha256} ({state})")
        lines.append(f"Approved by:  {asset.approved_by} at {asset.approved_at:%Y-%m-%d %H:%M %Z}")
    if asset.provider_revision or asset.provider_modified_at:
        modified = (
            f"{asset.provider_modified_at:%Y-%m-%d %H:%M %Z}"
            if asset.provider_modified_at
            else "unknown"
        )
        lines.append(f"Revision:     {asset.provider_revision or '-'} (modified {modified})")
    if assignment_count is not None:
        lines.append(f"Assigned to:  {assignment_count} lead slot(s)")
    if asset.notes:
        lines.append(f"Notes:        {asset.notes}")
    return lines
