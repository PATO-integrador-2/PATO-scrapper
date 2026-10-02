import fitz

from app.cleaner.text_utils import fix_mojibake


def extract_pdf(content: bytes):
    doc = fitz.open(stream=content, filetype='pdf')
    out = []
    for n, page in enumerate(doc, 1):
        text = ' '.join(page.get_text('text').split())
        text = fix_mojibake(text)  # ← fix del mojibake
        if text:
            out.append({'page_number': n, 'content': text})
    return out, len(doc)


def chunk_pages(pages, max_words=450, overlap=60):
    out = []
    for p in pages:
        words = p['content'].split()
        start = 0
        while start < len(words):
            end = min(len(words), start + max_words)
            out.append({
                'page_number': p['page_number'],
                'content': ' '.join(words[start:end]),
            })
            if end == len(words):
                break
            start = end - overlap
    return out