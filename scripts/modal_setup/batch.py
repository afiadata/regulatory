import modal
import io
from pathlib import Path


app = modal.App("regulatory-data")
vol = modal.Volume.from_name("regulatory-storage")

base_dir = Path("/data/ppb_pdfs")
output_dir = "output_dir" 

@app.local_entrypoint()
def main():
    base_dir = Path("../../data/ppb_pdfs")
    output_dir = "output_dir"

    with vol.batch_upload() as batch:
        batch.put_directory(str(base_dir), output_dir)
    print("✅ Upload completed!")