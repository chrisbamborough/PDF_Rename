import os
import re
import shutil
from pathlib import Path

from pypdf import PdfReader


def extract_year(text: str) -> str | None:
    """
    Find a plausible publication year in the text (between 1900 and 2100).
    Returns the first matching year as a string, or None.
    """
    if not text:
        return None

    matches = re.findall(r"\b(19|20)\d{2}\b", text)
    for m in matches:
        # re.findall returns only the capturing group if not careful,
        # so let's re-run with a full match:
        full_match = re.search(r"\b(19|20)\d{2}\b", text)
        if full_match:
            year = full_match.group(0)
            y = int(year)
            if 1900 <= y <= 2100:
                return year
    return None


def sanitize_for_filename(s: str, max_length: int = 80) -> str:
    """
    Remove characters that are invalid in filenames and truncate long strings.
    """
    s = s.strip()
    s = re.sub(r"[\\/<>:\"|?*]", "", s)      # remove illegal filename chars
    s = re.sub(r"\s+", " ", s)              # collapse whitespace
    if len(s) > max_length:
        s = s[:max_length].rstrip() + "…"
    return s


def guess_title_and_author_from_text(first_page_text: str) -> tuple[str | None, str | None]:
    """
    Very simple heuristic:
    - Treat the first non-empty, reasonably long line as the title.
    - Treat the next non-empty line that looks like an author list as authors.
    """
    if not first_page_text:
        return None, None

    lines = [ln.strip() for ln in first_page_text.splitlines()]
    lines = [ln for ln in lines if ln]  # drop empty

    title = None
    author = None

    # Guess title
    for ln in lines:
        if len(ln) > 10:  # somewhat long
            title = ln
            break

    # Guess author (after title)
    if title:
        try:
            start_idx = lines.index(title) + 1
        except ValueError:
            start_idx = 0
    else:
        start_idx = 0

    # Heuristics for author line:
    # - contains commas or 'and'
    # - not crazy long
    for ln in lines[start_idx: start_idx + 5]:
        if len(ln) < 80 and ("," in ln or " and " in ln or " " in ln):
            # crude check: mostly words with capital initials
            words = ln.split()
            cap_ratio = sum(w[0].isupper() for w in words if w) / max(len(words), 1)
            if cap_ratio > 0.4:
                author = ln
                break

    return title, author


def extract_metadata(pdf_path: Path) -> dict:
    """
    Extract title, author, and year from a PDF using metadata first,
    then fall back to simple heuristics on the first page text.
    """
    reader = PdfReader(str(pdf_path))

    meta = reader.metadata or {}
    title = getattr(meta, "title", None) or meta.get("/Title") if isinstance(meta, dict) else getattr(meta, "title", None)
    author = getattr(meta, "author", None) or meta.get("/Author") if isinstance(meta, dict) else getattr(meta, "author", None)

    # Try to grab some text from the first page for better guessing
    first_page_text = ""
    if reader.pages:
        try:
            first_page_text = reader.pages[0].extract_text() or ""
        except Exception:
            first_page_text = ""

    # Guess year from metadata + first page
    year = None
    meta_str = " ".join(str(v) for v in (title, author, meta) if v)
    year = extract_year(meta_str) or extract_year(first_page_text)

    # If title/author missing, try heuristic from text
    if not title or not author:
        guessed_title, guessed_author = guess_title_and_author_from_text(first_page_text)
        if not title and guessed_title:
            title = guessed_title
        if not author and guessed_author:
            author = guessed_author

    # Cleanup
    if isinstance(title, str):
        title = title.strip()
    if isinstance(author, str):
        author = author.strip()

    return {
        "title": title or "",
        "author": author or "",
        "year": year or "",
    }


def build_new_filename(info: dict, original_name: str) -> str:
    """
    Build a new filename like:
        '2020 - Smith - Interesting Paper.pdf'
    Falls back to parts of the original name if we can’t find everything.
    """
    title = info.get("title", "") or Path(original_name).stem
    author = info.get("author", "") or "UnknownAuthor"
    year = info.get("year", "") or "n.d."

    # Get first author surname if possible
    # e.g. "John Smith, Jane Doe" -> "Smith"
    author_surname = author
    if "," in author:
        first_author = author.split(",")[0]
        author_surname = first_author.split()[-1]
    else:
        author_surname = author.split()[-1] if author.split() else "Author"

    title_clean = sanitize_for_filename(title)
    author_clean = sanitize_for_filename(author_surname)
    year_clean = sanitize_for_filename(year)

    return f"{year_clean} - {author_clean} - {title_clean}.pdf"


def rename_pdfs_in_folder(folder: str, dry_run: bool = True, backup_folder: str | None = None):
    """
    Rename all PDF files in `folder` based on extracted metadata.

    dry_run=True -> only print what WOULD happen.
    If backup_folder is provided, original files are copied there before renaming.
    """
    folder_path = Path(folder)
    pdf_files = sorted(folder_path.glob("*.pdf"))

    if not pdf_files:
        print("No PDF files found.")
        return

    if backup_folder:
        backup_path = Path(backup_folder)
        backup_path.mkdir(parents=True, exist_ok=True)

    for pdf in pdf_files:
        try:
            info = extract_metadata(pdf)
            new_name = build_new_filename(info, pdf.name)
            new_path = pdf.with_name(new_name)

            print(f"\nFile: {pdf.name}")
            print(f"  -> Title:  {info.get('title') or 'N/A'}")
            print(f"  -> Author: {info.get('author') or 'N/A'}")
            print(f"  -> Year:   {info.get('year') or 'N/A'}")
            print(f"  => New filename: {new_name}")

            if not dry_run:
                if backup_folder:
                    shutil.copy2(pdf, backup_path / pdf.name)
                if new_path.exists():
                    # Avoid overwriting – add a counter
                    counter = 1
                    base = new_path.stem
                    while new_path.exists():
                        new_path = pdf.with_name(f"{base} ({counter}).pdf")
                        counter += 1
                pdf.rename(new_path)

        except Exception as e:
            print(f"Error processing {pdf.name}: {e}")


if __name__ == "__main__":
    # Example usage: change 'path/to/pdfs' to your actual folder.
    # First run as dry_run=True to check results.
    #rename_pdfs_in_folder("/Users/smthspce/Library/CloudStorage/OneDrive-UNSW/00_Arch Manu Personal_/Reading_/RENAME", dry_run=True)

    # When you're happy with the proposed names, set dry_run=False:
    rename_pdfs_in_folder("/Users/smthspce/Library/CloudStorage/OneDrive-UNSW/00_Arch Manu Personal_/Reading_/RENAME", dry_run=False)
    #rename_pdfs_in_folder("/Users/smthspce/Library/CloudStorage/OneDrive-UNSW/00_Arch Manu Personal_/Reading_/RENAME", dry_run=False, backup_folder="backup_originals")