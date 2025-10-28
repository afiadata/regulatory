import os
from pathlib import Path
import fitz 
from tqdm import tqdm

data_dir = Path("../../data/ppb_pdfs")
processed_dir = Path("../../data/processed_texts")
processed_dir.mkdir(exist_ok=True)

def preprocess_pdfs():
    for pdf_path in tqdm(data_dir.glob("**/*.pdf")):
        text = ""
        with fitz.open(pdf_path) as doc:
            for page in doc:
                text += page.get_text("text")
        output_file = processed_dir / f"{pdf_path.stem}.txt"
        output_file.write_text(text, encoding="utf-8")
    print("✅ Preprocessing complete.")

if __name__ == "__main__":
    preprocess_pdfs()