import os
import modal
from pathlib import Path
from tqdm import tqdm
import fitz
import re

app = modal.App("regulatory-data")
volume = modal.Volume.from_name("regulatory-storage")

image = (
    modal.Image.debian_slim().pip_install("PyPDF2","pymupdf","tqdm")
)

# def clean_text(text: str) -> str:
#     text = re.sub(r'\s+', ' ', text)  # collapse whitespace
#     text = re.sub(r'[^\x00-\x7F]+', ' ', text)  # remove non-ASCII chars
#     return text.strip()

@app.function(image=image, volumes={"/vol":volume}, timeout=300)
def preprocess_pdf():
    base_dir = Path("/vol/output_dir")
    processed_dir = Path("/vol/processed_texts")
    processed_dir.mkdir(parents = True, exist_ok = True)

    # Get PDFS
    pdf_files = [str(p) for p in Path(base_dir).rglob("**/*.pdf")]

    for pdf_path in base_dir.glob("**/*.pdf"):
        text = ""
        with fitz.open(pdf_path) as doc:
            for page in doc:
                text += page.get_text("text")
        (processed_dir / f"{pdf_path.stem}.txt").write_text(text, encoding="utf-8")

    print(f"Cleaned {len(pdf_files)} files. Saved to {processed_dir}.")

if __name__ == "__main__":
    with app.run():
        preprocess_pdf.remote()

    