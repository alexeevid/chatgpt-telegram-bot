import io
import pandas as pd

def extract_text(fileobj: io.BytesIO, filename: str) -> str:
    filename = filename.lower()

    if filename.endswith('.pdf'):
        import pdfplumber
        with pdfplumber.open(fileobj) as pdf:
            return '\n'.join(p.extract_text() or '' for p in pdf.pages)

    elif filename.endswith('.docx'):
        from docx import Document
        document = Document(fileobj)
        return '\n'.join(p.text for p in document.paragraphs)

    elif filename.endswith(('.csv', '.tsv')):
        df = pd.read_csv(fileobj, nrows=50)
        return df.to_markdown()

    elif filename.endswith('.txt'):
        return fileobj.read().decode('utf-8', errors='ignore')

    else:
        raise ValueError(f"Unsupported file format: {filename}")
