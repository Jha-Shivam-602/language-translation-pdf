import os
import hashlib
from pathlib import Path
import streamlit as st
import pymupdf
from deep_translator import GoogleTranslator, MyMemoryTranslator
import logging
import concurrent.futures
import time
import re
import translation_cache
from pdf_translator import COLOR_MAP, get_blocks, write_translated_page

DEFAULT_PAGES_PER_LOAD = 1

LANGUAGE_OPTIONS = {
    "English": "en",
    "Assamese": "as",
    "Bengali": "bn",
    "Bhojpuri": "bho",
    "Dogri": "doi",
    "Gujarati": "gu",
    "Hindi": "hi",
    "Kannada": "kn",
    "Kashmiri": "ks",
    "Konkani": "gom",
    "Maithili": "mai",
    "Malayalam": "ml",
    "Marathi": "mr",
    "Meiteilon": "mni-Mtei",
    "Nepali": "ne",
    "Odia": "or",
    "Punjabi": "pa",
    "Sanskrit": "sa",
    "Sindhi": "sd",
    "Tamil": "ta",
    "Telugu": "te",
    "Urdu": "ur",
    "简体中文": "zh-CN",
    "繁體中文": "zh-TW",
    "日本語": "ja",
    "한국어": "ko",
    "Español": "es",
    "Français": "fr",
    "Deutsch": "de"
}

SOURCE_LANGUAGE_OPTIONS = {"Auto": "auto"}
SOURCE_LANGUAGE_OPTIONS.update(LANGUAGE_OPTIONS)

def get_cache_dir():
    cache_dir = Path('.cached')
    cache_dir.mkdir(exist_ok=True)
    return cache_dir

def get_cache_key(doc_info: dict, page_num: int, target_lang: str, text_content: str):
    payload = "\x1f".join([
        str(doc_info.get('title', '')),
        str(doc_info.get('author', '')),
        str(doc_info.get('pagecount', '')),
        str(page_num),
        target_lang,
        text_content,
    ])
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return f"{digest}.pdf"

def get_cached_translation(cache_key: str) -> pymupdf.Document:
    cache_path = get_cache_dir() / cache_key
    if cache_path.exists():
        try:
            return pymupdf.open(str(cache_path))
        except Exception as e:
            logging.error(f"Error loading cache: {str(e)}")
            return None
    return None

def save_translation_cache(doc: pymupdf.Document, cache_key: str):
    cache_path = get_cache_dir() / cache_key
    doc.save(str(cache_path))

# --- NEW TRANSLATION ENGINE LOGIC ---

def clean_and_assemble_text(text: str) -> str:
    """Assembles broken PDF text blocks into continuous sentences."""
    if not text or not text.strip():
        return ""
    
    # Replace single line breaks inside a block with a space
    cleaned = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    # Remove hyphenations at the end of lines
    cleaned = re.sub(r'(\w+)-\s+(\w+)', r'\1\2', cleaned)
    # Strip redundant spacing
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()

def safe_translate_text(text: str, source: str, target: str, retries=3) -> str:
    """Translates a single block with exponential backoff and provider fallbacks."""
    cleaned_text = clean_and_assemble_text(text)
    if not cleaned_text or cleaned_text.isdigit():
        return text

    # Attempt 1: Google Translator (Primary)
    for attempt in range(retries):
        try:
            translator = GoogleTranslator(source=source, target=target)
            return translator.translate(cleaned_text)
        except Exception as e:
            logging.warning(f"Google Translate attempt {attempt + 1} failed: {e}")
            time.sleep(1.5 ** attempt) 
            
    # Attempt 2: MyMemory Translator (Fallback)
    try:
        logging.info("Attempting backup translation provider (MyMemory)...")
        backup_translator = MyMemoryTranslator(source=source, target=target)
        return backup_translator.translate(cleaned_text)
    except Exception as final_exc:
        logging.error(f"All translation providers exhausted for block. Error: {final_exc}")
        return text 

def translate_blocks(texts, source_lang: str, target_lang: str):
    """Processes, caches, and translates blocks concurrently."""
    source = source_lang if source_lang else "auto"
    target = target_lang

    results = [None] * len(texts)
    misses_idx = []
    misses_text = []

    # 1. Local Cache Lookup
    for i, text in enumerate(texts):
        cleaned = clean_and_assemble_text(text)
        if not cleaned:
            results[i] = text
            continue
            
        cached = translation_cache.get("google", source, target, "default", cleaned)
        if cached is not None:
            results[i] = cached
        else:
            misses_idx.append(i)
            misses_text.append(cleaned)

    failed = 0
    
    # 2. Parallel translation for cache misses
    if misses_text:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_idx = {
                executor.submit(safe_translate_text, txt, source, target): idx 
                for idx, txt in zip(misses_idx, misses_text)
            }
            
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                original_clean_text = misses_text[misses_idx.index(idx)]
                try:
                    translated_out = future.result()
                    results[idx] = translated_out
                    
                    if translated_out != original_clean_text:
                        translation_cache.put("google", source, target, "default", original_clean_text, translated_out)
                except Exception as exc:
                    logging.error(f"Thread execution failed for block index {idx}: {exc}")
                    results[idx] = texts[idx]
                    failed += 1

    return results, failed

def translate_pdf_pages(doc, start_page, num_pages, source_lang, target_lang, text_color):
    translated_pages = []
    end_page = min(start_page + num_pages, doc.page_count)
    total_pages = end_page - start_page

    progress_bar = st.progress(0)

    for i, page_num in enumerate(range(start_page, end_page)):
        page = doc[page_num]
        text_content = page.get_text("text")

        cache_key = get_cache_key(doc.metadata, page_num, target_lang, text_content)
        cached_doc = get_cached_translation(cache_key)

        if cached_doc is not None:
            translated_pages.append(cached_doc)
        else:
            new_doc = pymupdf.open()
            new_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)
            page = new_doc[0]

            blocks = get_blocks(page)
            raw_texts = [b[4] for b in blocks]
            
            translated_texts, failed = translate_blocks(
                raw_texts, source_lang, target_lang
            )

            write_translated_page(page, blocks, translated_texts, text_color=text_color)
            save_translation_cache(new_doc, cache_key)
            translated_pages.append(new_doc)

        progress = (i + 1) / total_pages
        progress_bar.progress(progress)

    progress_bar.empty()
    return translated_pages

def get_page_image(page, scale=2):
    zoom = scale
    mat = pymupdf.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False, colorspace="rgb")
    return pix

def translate_all_pages(input_doc, output_doc, source_lang, target_lang, text_color, output_path):
    total_pages = input_doc.page_count

    translated_pages = translate_pdf_pages(
        input_doc, 0, total_pages, source_lang, target_lang, text_color
    )

    for trans_doc in translated_pages:
        output_doc.insert_pdf(trans_doc)

    output_doc.save(output_path, garbage=4, deflate=True, clean=True)
    return output_doc

def init_session_state():
    if 'current_page' not in st.session_state:
        st.session_state.current_page = 0
    if 'all_translated' not in st.session_state:
        st.session_state.all_translated = False
    if 'translated_doc' not in st.session_state:
        st.session_state.translated_doc = None
    if 'previous_file' not in st.session_state:
        st.session_state.previous_file = None
    if 'last_translation_settings' not in st.session_state:
        st.session_state.last_translation_settings = None
    if 'view_translated_path' not in st.session_state:
        st.session_state.view_translated_path = None

def main():
    st.set_page_config(layout="wide", page_title="PDF Translator")

    st.markdown("""
        <style>
            .block-container { padding-top: 4rem; padding-bottom: 0rem; }
            .stImage img { max-height: 70vh; object-fit: contain; }
        </style>
    """, unsafe_allow_html=True)

    init_session_state()

    with st.sidebar:
        uploaded_file = st.file_uploader("Choose a PDF file", type="pdf")

        if uploaded_file is not None and (st.session_state.previous_file is None or uploaded_file.name != st.session_state.previous_file):
            st.session_state.current_page = 0
            st.session_state.all_translated = False
            st.session_state.translated_doc = None
            st.session_state.previous_file = uploaded_file.name
            st.rerun()

        source_lang_name = st.selectbox("Source Language", options=list(SOURCE_LANGUAGE_OPTIONS.keys()), index=0)
        source_lang = SOURCE_LANGUAGE_OPTIONS[source_lang_name]

        pages_per_load = st.number_input("Pages per load", min_value=1, max_value=5, value=DEFAULT_PAGES_PER_LOAD)

        text_color = st.selectbox("Translation Color", options=list(COLOR_MAP.keys()), index=0)

        target_lang = st.selectbox("Target Language", options=list(LANGUAGE_OPTIONS.keys()), index=0)
        target_lang_code = LANGUAGE_OPTIONS[target_lang]

    current_settings = (source_lang, target_lang_code, text_color, pages_per_load)
    if (
        st.session_state.all_translated
        and st.session_state.last_translation_settings is not None
        and current_settings != st.session_state.last_translation_settings
    ):
        st.session_state.all_translated = False
        st.session_state.translated_doc = None

    if uploaded_file is None:
        st.markdown(
            """
            <div style="
                background-color: rgba(28, 131, 225, 0.1);
                border: 1px solid rgba(28, 131, 225, 0.3);
                border-radius: 0.5rem;
                padding: 1rem;
                color: #7cb9ff;
            ">
                Upload a PDF file from the sidebar to get started.
            </div>
            """,
            unsafe_allow_html=True
        )
        return

    doc_bytes = uploaded_file.read()
    doc = pymupdf.open(stream=doc_bytes, filetype="pdf")

    # PDF Viewer
    col1, col2 = st.columns(2)

    # Original PDF
    with col1:
        for page_num in range(
            st.session_state.current_page,
            min(st.session_state.current_page + pages_per_load, doc.page_count)
        ):
            page = doc[page_num]
            pix = get_page_image(page)
            st.image(pix.tobytes(), use_container_width=True)

    # Translated PDF
    with col2:
        try:
            translated_pages = translate_pdf_pages(
                doc,
                st.session_state.current_page,
                pages_per_load,
                source_lang,
                target_lang_code,
                text_color
            )

            for trans_doc in translated_pages:
                page = trans_doc[0]
                pix = get_page_image(page)
                st.image(pix.tobytes(), use_container_width=True)

            view_doc = pymupdf.open()
            for trans_doc in translated_pages:
                view_doc.insert_pdf(trans_doc)
            view_path = f"current_view_{uploaded_file.name}"
            view_doc.save(view_path, garbage=4, deflate=True, clean=True)
            st.session_state.view_translated_path = view_path

        except Exception as e:
            st.error(f"Translation error: {str(e)}")
            return

    # Footer Buttons
    button_col1, button_col2, button_col3, button_col4 = st.columns(4)

    with button_col1:
        if st.session_state.current_page > 0:
            if st.button("Previous Pages", use_container_width=True):
                st.session_state.current_page = max(
                    0,
                    st.session_state.current_page - pages_per_load
                )
                st.rerun()
        else:
            st.button(
                "Previous Pages",
                disabled=True,
                use_container_width=True
            )

    with button_col2:
        if st.session_state.current_page + pages_per_load < doc.page_count:
            if st.button("Next Pages", use_container_width=True):
                st.session_state.current_page = min(
                    max(0, doc.page_count - pages_per_load),
                    st.session_state.current_page + pages_per_load
                )
                st.rerun()
        else:
            st.button(
                "Next Pages",
                disabled=True,
                use_container_width=True
            )

    with button_col3:
        if st.button(
            "Translate All",
            disabled=st.session_state.all_translated,
            use_container_width=True
        ):
            try:
                output_doc = pymupdf.open()
                output_path = f"translated_{uploaded_file.name}"

                output_doc = translate_all_pages(
                    doc,
                    output_doc,
                    source_lang,
                    target_lang_code,
                    text_color,
                    output_path
                )

                st.session_state.all_translated = True
                st.session_state.translated_doc = output_path
                st.session_state.last_translation_settings = current_settings
                st.rerun()

            except Exception as e:
                st.error(f"Translation error: {str(e)}")
                return

    with button_col4:
        if st.session_state.all_translated and st.session_state.translated_doc:
            with open(st.session_state.translated_doc, "rb") as file:
                st.download_button(
                    "Download",
                    file,
                    file_name=f"translated_{uploaded_file.name}",
                    mime="application/pdf",
                    use_container_width=True
                )
        elif st.session_state.view_translated_path:
            with open(st.session_state.view_translated_path, "rb") as file:
                st.download_button(
                    "Download",
                    file,
                    file_name=f"translated_pages_{uploaded_file.name}",
                    mime="application/pdf",
                    use_container_width=True
                )
        else:
            st.button(
                "Download",
                disabled=True,
                use_container_width=True
            )

if __name__ == "__main__":
    main()