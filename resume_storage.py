"""Stable resume paths, independent of the process working directory."""
from pathlib import Path


def resume_path(base_dir, config):
    uploaded = Path(base_dir) / 'uploads' / 'resume.pdf'
    if uploaded.is_file():
        return str(uploaded)
    raw = (config.get('user_profile') or {}).get('resume_path') or 'resume.pdf'
    path = Path(str(raw).strip() or 'resume.pdf').expanduser()
    return str(path if path.is_absolute() else Path(base_dir) / path)
