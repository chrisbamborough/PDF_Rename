import os
import re
import subprocess
import shutil
from pathlib import Path

from pypdf import PdfReader

try:
    import requests
except ImportError:
    requests = None
    # If you want Crossref support, run: python -m pip install requests


# ------------ Helpers ------------ #

def extract_year(text: str) -> str | None:
    """
    Find a plausible publication year (1900-2100) in text.
    Looks for plain years and '© 2019'-style patterns.
    """
    if not text:
        return None

    # Patterns like "© 2019", "(c) 2018", "Copyright 2020"
    copyright_match = re.search(
        r"(?:©|\(c\)|copyright)\s*(19|20)\d{2}",
        text,
        flags=re.IGNORECASE
    )
    if copyright_match:
        year = re.search(r"(19|20)\d{2}", copyright_match.group(0))
        if year:
            return year.group(0)

    # Plain 4-digit years
    for match in re.findall(r"\b(19|20)\d{2}\b", text):
        # findall can get only the group, so re-search full match around it
        full = re.search(rf"\b{match}\d{{2}}\b", text)
        if full:
            y = int(full.group(0))
            if 1900 <= y <= 2100:
                return full.group(0)

    return None


def sanitize_for_filename(s: str, max_length: int = 80) -> str:
    """
    Remove characters that are invalid in filenames and truncate long strings.
    """
    s = s.strip()
    s = re.sub(r"[\\/<>:\"|?*]", "", s)      # illegal filename chars
    s = re.sub(r"\s+", " ", s)              # collapse whitespace
    if len(s) > max_length:
        s = s[:max_length].rstrip() + "…"
    return s


def find_doi(text: str) -> str | None:
    """
    Try to locate a DOI in text.
    """
    if not text:
        return None

    # Rough but common DOI pattern
    match = re.search(r"\b10\.\d{4,9}/\S+\b", text)
    if match:
        # strip trailing punctuation if any
        doi = match.group(0).rstrip(").,;")
        return doi
    return None


def fetch_metadata_from_crossref(doi: str) -> dict | None:
    """
    Query Crossref for a DOI and return standardised metadata dict.
    Requires `requests` and internet access.
    """
    if not requests or not doi:
        return None

    url = f"https://api.crossref.org/works/{doi}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json().get("message", {})

        title = ""
        if isinstance(data.get("title"), list) and data["title"]:
            title = data["title"][0]

        authors = data.get("author", [])
        author_str = ""
        if authors:
            names = []
            for a in authors:
                given = a.get("given", "")
                family = a.get("family", "")
                if family and given:
                    names.append(f"{given} {family}")
                elif family:
                    names.append(family)
            author_str = ", ".join(names)

        year = ""
        for key in ("published-print", "published-online", "issued"):
            if key in data and "date-parts" in data[key]:
                parts = data[key]["date-parts"][0]
                if parts:
                    year = str(parts[0])
                    break

        return {
            "title": title or "",
            "author": author_str or "",
            "year": year or "",
            "source": "crossref",
        }
    except Exception:
        return None


def guess_title_and_author_from_text(full_text: str) -> tuple[str | None, str | None]:
    """
    Better heuristic:
    - Use the block of lines from the top until 'Abstract' (if found).
    - Choose the longest 'reasonable' line as title.
    - Choose a nearby line that looks like an author list.
    """
    if not full_text:
        return None, None

    lines = [ln.strip() for ln in full_text.splitlines()]
    lines = [ln for ln in lines if ln]  # drop empty

    if not lines:
        return None, None

    # Find "Abstract" marker
    abstract_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().lower() in ("abstract", "abstrakt") or ln.lower().startswith("abstract "):
            abstract_idx = i
            break

    if abstract_idx is not None:
        candidate_block = lines[:abstract_idx]
    else:
        # Just take the top block of lines
        candidate_block = lines[:30]

    # Filter out very short or obviously junk lines
    candidate_block = [ln for ln in candidate_block if len(ln) > 5]

    if not candidate_block:
        return None, None

    # Guess title: longest line in this block, but not absurdly long
    title = max(candidate_block, key=len)
    if len(title) > 200:  # if it's insanely long, bail
        title = None

    # Guess author:
    # Look near the title (a few lines above/below) for an "author-ish" line:
    # - shorter than 100 chars
    # - has commas or 'and'
    # - high proportion of capitalised words
    author = None
    if title:
        try:
            t_idx = candidate_block.index(title)
        except ValueError:
            t_idx = 0
        window = candidate_block[max(0, t_idx - 4): t_idx + 6]
    else:
        window = candidate_block[:10]

    for ln in window:
        if len(ln) > 100:
            continue
        if not ("," in ln or " and " in ln or " " in ln):
            continue
        words = ln.replace(",", " ").split()
        if not words:
            continue
        cap_ratio = sum(w[0].isupper() for w in words if w) / max(len(words), 1)
        if cap_ratio > 0.4:
            author = ln
            break

    return title, author


def extract_metadata(pdf_path: Path, use_crossref: bool = True, max_pages: int = 3) -> dict:
    """
    Extract title, author, and year using:
    1. PDF metadata
    2. DOI + Crossref (if DOI found and use_crossref=True)
    3. Heuristic analysis of first N pages
    """
    reader = PdfReader(str(pdf_path))

    meta = reader.metadata or {}
    # pypdf 3.x+: metadata is a DocumentInformation object
    title = getattr(meta, "title", None) or (meta.get("/Title") if isinstance(meta, dict) else None)
    author = getattr(meta, "author", None) or (meta.get("/Author") if isinstance(meta, dict) else None)

    # Collect text from first N pages
    full_text = ""
    for i, page in enumerate(reader.pages[:max_pages]):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        full_text += "\n" + page_text

    # Year from metadata or text
    combined_meta_str = " ".join(str(v) for v in (title, author, meta) if v)
    year = extract_year(combined_meta_str) or extract_year(full_text)

    # Try Crossref via DOI if allowed and we don't already have good info
    doi = None
    crossref_info = None
    if use_crossref:
        doi = find_doi(full_text)
        if doi:
            crossref_info = fetch_metadata_from_crossref(doi)

    if crossref_info:
        title = crossref_info["title"] or title
        author = crossref_info["author"] or author
        year = crossref_info["year"] or year

    # If title/author still missing, try heuristic from text
    if not title or not author:
        guessed_title, guessed_author = guess_title_and_author_from_text(full_text)
        if not title and guessed_title:
            title = guessed_title
        if not author and guessed_author:
            author = guessed_author

    # Cleanup
    title = (title or "").strip()
    author = (author or "").strip()
    year = (year or "").strip()

    return {
        "title": title,
        "author": author,
        "year": year,
        "doi": doi or "",
        "source": crossref_info["source"] if crossref_info else "local",
    }


def build_new_filename(info: dict) -> str | None:
    """
    Build a new filename *only* from extracted metadata.

    Rules:
    - If we have year + author + title:   'YYYY - Surname - Title.pdf'
    - If we have year + title:           'YYYY - Title.pdf'
    - If we have title only:             'Title.pdf'
    - Otherwise: return None (caller will skip renaming).
    """
    title = (info.get("title") or "").strip()
    author = (info.get("author") or "").strip()
    year = (info.get("year") or "").strip()

    # If we truly have nothing, do not rename this file
    if not title and not author and not year:
        return None

    title_clean = sanitize_for_filename(title) if title else ""
    year_clean = sanitize_for_filename(year) if year else ""

    # Derive first author surname, if any
    author_surname = ""
    if author:
        if "," in author:
            first_author = author.split(",")[0]
            author_surname = first_author.split()[-1]
        else:
            parts = author.split()
            if parts:
                author_surname = parts[-1]

    author_clean = sanitize_for_filename(author_surname) if author_surname else ""

    parts: list[str] = []

    if year_clean:
        parts.append(year_clean)
    if author_clean:
        parts.append(author_clean)
    if title_clean:
        parts.append(title_clean)

    # If we only have a bare year with no other info, that's not useful enough: skip
    if len(parts) == 1 and parts[0] == year_clean:
        return None

    if not parts:
        return None

    return " - ".join(parts) + ".pdf"


def rename_pdfs_in_folder(
    folder: str,
    dry_run: bool = True,
    backup_folder: str | None = None,
    use_crossref: bool = True,
    max_pages: int = 3,
):
    folder_path = Path(folder)
    pdf_files = sorted(folder_path.glob("*.pdf"))

    if backup_folder:
        backup_path = Path(backup_folder)
        backup_path.mkdir(parents=True, exist_ok=True)
    else:
        backup_path = None

    for pdf in pdf_files:
        try:
            info = extract_metadata(pdf, use_crossref=use_crossref, max_pages=max_pages)

            print(f"\nFile: {pdf.name}")
            print(f"  -> Title:  {info.get('title') or 'N/A'}")
            print(f"  -> Author: {info.get('author') or 'N/A'}")
            print(f"  -> Year:   {info.get('year') or 'N/A'}")

            new_name = build_new_filename(info)

            if not new_name:
                print("  !! Not enough reliable metadata, skipping rename.")
                continue

            new_path = pdf.with_name(new_name)
            print(f"  => New filename: {new_name}")

            if not dry_run:
                if backup_path is not None:
                    # Save a copy of the original file in the backup folder
                    shutil.copy2(pdf, backup_path / pdf.name)

                if new_path.exists():
                    # avoid collisions
                    counter = 1
                    base = new_path.stem
                    while new_path.exists():
                        new_path = pdf.with_name(f"{base} ({counter}).pdf")
                        counter += 1

                pdf.rename(new_path)

        except Exception as e:
            print(f"Error processing {pdf.name}: {e}")


def choose_folder_in_finder() -> str | None:
    """
    Open a native Finder folder picker (macOS) and return the selected path.
    Returns None if the user cancels or selection fails.
    """
    script = 'POSIX path of (choose folder with prompt "Select a folder of PDFs to rename")'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        # User cancellation is returned as a non-zero exit code.
        return None

    folder = result.stdout.strip()
    return folder or None


if __name__ == "__main__":
    selected_folder = choose_folder_in_finder()
    if not selected_folder:
        print("No folder selected. Exiting.")
        raise SystemExit(0)

    # First run as dry_run=True to see what it would do
    rename_pdfs_in_folder(
        selected_folder,
        dry_run=False,
        use_crossref=True,   # set False if you want purely offline behaviour
        max_pages=10,
    )