from pathlib import Path
from langchain_core.documents import Document


def load_documents(root: Path):
    if not root.is_dir():
        raise ValueError(f"Knowledge directory does not exist: {root}")
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in {".txt", ".md", ".pdf"}
        ):
            continue
        if path.name == "README.md":
            continue
        source = path.relative_to(root).as_posix()
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            for page, content in enumerate(PdfReader(path).pages, 1):
                yield Document(
                    page_content=content.extract_text() or "",
                    metadata={"source": source, "page": page},
                )
        else:
            yield Document(
                page_content=path.read_text(encoding="utf-8-sig"), metadata={"source": source}
            )
