import os
import json
from pathlib import Path
import fitz  # PyMuPDF
from collections import Counter, defaultdict
import re
import unicodedata
from langdetect import detect, DetectorFactory
DetectorFactory.seed = 0  # for reproducibility

# Constants for heading levels
HEADING_LEVELS = ["H1", "H2", "H3"]

# Patterns that look like headings in different languages
# We spent a lot of time testing these with various PDFs
HEADING_PATTERNS = [
    r"^\d+([\.|\)]| )",  # 1. or 1) or 1 
    r"^第[一二三四五六七八九十百千万]+[章节]",  # Chinese/Japanese: 第1章, 第三节
    r"^Section \d+",  # Section 1
    r"^Chapter \d+",  # Chapter 1
    r"^[A-Z]\. ",  # A. Introduction
    r"^\d+\.\d+",  # 1.1, 2.3
    r"^\d+、",  # Japanese/Chinese list: 1、
    r"^\([a-zA-Z]\)",  # (a) (A)
]
HEADING_REGEXES = [re.compile(p) for p in HEADING_PATTERNS]

# Unicode ranges for Chinese, Japanese, Korean characters
# These languages don't use spaces between words like English does
CJK_RANGES = [
    (0x4E00, 0x9FFF),  # Chinese characters
    (0x3040, 0x309F),  # Japanese hiragana
    (0x30A0, 0x30FF),  # Japanese katakana
    (0xAC00, 0xD7AF),  # Korean hangul
]

def is_cjk(text):
    """Check if text contains Chinese, Japanese, or Korean characters"""
    for ch in text:
        code = ord(ch)
        for start, end in CJK_RANGES:
            if start <= code <= end:
                return True
    return False

def is_rtl(text):
    """Check if text is right-to-left (Arabic, Hebrew, etc.)"""
    for ch in text:
        if unicodedata.bidirectional(ch) in ("R", "AL", "AN"):
            return True
    return False

def normalize_text(text):
    """Clean up text by normalizing unicode characters"""
    return unicodedata.normalize("NFKC", text)

def matches_heading_pattern(text):
    """Check if text matches any of our heading patterns"""
    for regex in HEADING_REGEXES:
        if regex.match(text):
            return True
    return False

def is_bold(span):
    """Check if text span is bold based on font name or flags"""
    return 'Bold' in span['font'] or (span.get('flags', 0) & 2) != 0

def is_italic(span):
    """Check if text span is italic based on font name or flags"""
    return 'Italic' in span['font'] or (span.get('flags', 0) & 1) != 0

def is_centered(span, page_width):
    """Check if text span is centered on the page"""
    left, top, right, bottom = span['bbox']
    center = (left + right) / 2
    return abs(center - page_width / 2) < page_width * 0.15

def is_all_caps(text):
    """Check if text is all uppercase (like a heading)"""
    return text.isupper() and len(text) > 2

def is_title_case(text):
    """Check if text is in title case (like a heading)"""
    return bool(re.match(r'^[A-Z][a-z]+( [A-Z][a-z]+)*$', text))

def detect_language(text):
    """Figure out what language the text is in"""
    try:
        return detect(text)
    except:
        return 'unknown'

def score_heading_candidate(span, page_width):
    """Score how likely a text span is to be a heading"""
    text = normalize_text(span['text'])
    score = 0
    
    # Font size is a big indicator - larger text is more likely to be a heading
    score += span['size'] * 2
    
    # Bold text is often used for headings
    if is_bold(span):
        score += 3
    
    # Italic text can also indicate headings
    if is_italic(span):
        score += 1
    
    # Centered text is often a heading
    if is_centered(span, page_width):
        score += 2
    
    # If it matches our heading patterns (like "1. Introduction"), that's a strong signal
    if matches_heading_pattern(text):
        score += 4
    
    # Handle different languages - some languages don't use capitalization the same way
    cjk = is_cjk(text)
    rtl = is_rtl(text)
    lang = detect_language(text)
    
    # Only check capitalization for languages that use it
    if not cjk and not rtl and lang in ['en', 'fr', 'de', 'es', 'it', 'pt', 'nl', 'sv', 'da', 'fi', 'no', 'pl', 'tr', 'ro', 'cs', 'sk', 'hu', 'sl', 'hr', 'lt', 'lv', 'et', 'bg', 'ca', 'ga', 'mt', 'is', 'sq', 'mk', 'bs', 'sr', 'eu', 'gl', 'af', 'sw', 'zu', 'xh', 'st', 'tn', 'ts', 'ss', 've', 'nr', 'ny', 'mg', 'so', 'rw', 'rn', 'kg', 'lu', 'lg', 'ak', 'ee', 'tw', 'ha', 'yo', 'ig', 'am', 'om', 'ti', 'aa', 'ss', 'tn', 'ts', 've', 'xh', 'zu']:
        if is_all_caps(text):
            score += 2
        if is_title_case(text):
            score += 1
    
    # Shorter text is more likely to be a heading
    if len(text) < 40:
        score += 1
    
    # But very short text (like single letters) is probably not a heading
    if len(text) < 4:
        score -= 2
    
    return score

def extract_headings_from_page(page):
    """Extract all potential headings from a single page"""
    blocks = page.get_text("dict")['blocks']
    headings = []
    page_width = page.rect.width
    
    # Go through all text blocks on the page
    for b in blocks:
        if 'lines' not in b:
            continue
        for l in b['lines']:
            for s in l['spans']:
                text = normalize_text(s['text'].strip())
                if not text or len(text) < 2:
                    continue
                
                # Create a span object with all the info we need
                span = {
                    'text': text,
                    'size': s['size'],
                    'flags': s['flags'],
                    'font': s['font'],
                    'bbox': s['bbox'],
                }
                
                # Score this span to see if it's likely a heading
                span['score'] = score_heading_candidate(span, page_width)
                headings.append(span)
    
    return headings

def guess_title_and_headings(all_headings_by_page):
    """Figure out the title and organize headings into a proper outline"""
    # Collect all spans from all pages
    all_spans = []
    for page_num, spans in all_headings_by_page.items():
        for s in spans:
            s['page'] = page_num
            all_spans.append(s)
    
    if not all_spans:
        return "", []
    
    # The title is usually the highest-scoring text on the first page
    first_page_spans = all_headings_by_page.get(1, [])
    if first_page_spans:
        title_span = max(first_page_spans, key=lambda s: s['score'])
        title = title_span['text']
    else:
        # Fallback to the first span if no first page
        title = all_spans[0]['text']
    
    # Sort all spans by score (highest first) and then by font size
    sorted_spans = sorted(all_spans, key=lambda s: (-s['score'], -s['size']))
    
    # Assign heading levels based on scores
    # H1 needs score 10+, H2 needs 8+, H3 needs 6+
    outline = []
    used = set()  # Avoid duplicates
    
    for level in HEADING_LEVELS:
        for s in sorted_spans:
            key = (s['text'], s['page'])
            if key in used:
                continue
            if s['score'] >= 10 - 2 * HEADING_LEVELS.index(level):  # H1: 10+, H2: 8+, H3: 6+
                outline.append({
                    'level': level,
                    'text': s['text'],
                    'page': s['page']
                })
                used.add(key)
    
    return title, outline

def process_pdfs():
    """Main function to process all PDFs in the input directory"""
    input_dir = Path("/app/input")
    output_dir = Path("/app/output")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find all PDF files in the input directory
    pdf_files = list(input_dir.glob("*.pdf"))
    
    for pdf_file in pdf_files:
        try:
            # Open the PDF and extract headings from each page
            doc = fitz.open(pdf_file)
            all_headings_by_page = defaultdict(list)
            
            for i, page in enumerate(doc, 1):
                spans = extract_headings_from_page(page)
                all_headings_by_page[i].extend(spans)
            
            # Figure out the title and create the outline
            title, outline = guess_title_and_headings(all_headings_by_page)
            
            output = {
                "title": title,
                "outline": outline
            }
        except Exception as e:
            # If something goes wrong, return empty results with error info
            output = {"title": "", "outline": [], "error": str(e)}
        
        # Write the results to a JSON file
        output_file = output_dir / f"{pdf_file.stem}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"Processed {pdf_file.name} -> {output_file.name}")

if __name__ == "__main__":
    print("Starting PDF processing...")
    process_pdfs()
    print("Finished processing all PDFs!")