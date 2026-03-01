import streamlit as st
import fitz  # PyMuPDF
import time
import os
import threading
from queue import Queue, Empty
import tempfile
from gtts import gTTS
import io
import base64

# Configuración inicial
st.set_page_config(layout="wide", page_title="PDF Audio Reader", page_icon="📖")

# Inicializar session state
if 'words_cache' not in st.session_state:
    st.session_state.words_cache = {}
if 'current_page' not in st.session_state:
    st.session_state.current_page = 1
if 'is_reading' not in st.session_state:
    st.session_state.is_reading = False
if 'word_index' not in st.session_state:
    st.session_state.word_index = 0
if 'start_page' not in st.session_state:
    st.session_state.start_page = 1
if 'end_page' not in st.session_state:
    st.session_state.end_page = 1
if 'all_words_text' not in st.session_state:
    st.session_state.all_words_text = ""
if 'all_words_list' not in st.session_state:
    st.session_state.all_words_list = []
if 'page_word_ranges' not in st.session_state:
    st.session_state.page_word_ranges = {}
if 'words_by_page' not in st.session_state:
    st.session_state.words_by_page = {}
if 'audio_thread' not in st.session_state:
    st.session_state.audio_thread = None
if 'stop_audio' not in st.session_state:
    st.session_state.stop_audio = False
if 'word_queue' not in st.session_state:
    st.session_state.word_queue = Queue()
if 'pdf_path' not in st.session_state:
    st.session_state.pdf_path = None
if 'pdf_name' not in st.session_state:
    st.session_state.pdf_name = None
if 'total_pages' not in st.session_state:
    st.session_state.total_pages = 0
if 'audio_placeholder' not in st.session_state:
    st.session_state.audio_placeholder = None

def extract_text_with_positions(pdf_path, page_num):
    """Extrae texto con sus posiciones en la página"""
    doc = fitz.open(pdf_path)
    page = doc[page_num - 1]
    words = page.get_text("words")
    doc.close()
    return words

def highlight_words_on_page(pdf_path, page_num, word_indices_to_highlight, words_list):
    """Crea una imagen del PDF con palabras resaltadas específicas"""
    doc = fitz.open(pdf_path)
    page = doc[page_num - 1]
    
    # Limpiar anotaciones existentes
    for annot in page.annots():
        page.delete_annot(annot)
    
    # Resaltar palabras
    for idx in word_indices_to_highlight:
        if idx < len(words_list):
            word_info = words_list[idx]
            rect = fitz.Rect(word_info[0], word_info[1], word_info[2], word_info[3])
            highlight = page.add_highlight_annot(rect)
            highlight.set_colors(stroke=[1, 1, 0])  # Color amarillo
            highlight.update()
    
    # Convertir página a imagen
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    img_data = pix.tobytes("png")
    doc.close()
    
    return img_data

def play_audio_word(word):
    """Reproduce una palabra usando HTML5 Audio con base64"""
    try:
        tts = gTTS(text=word, lang='en', slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        audio_bytes = fp.read()
        audio_base64 = base64.b64encode(audio_bytes).decode()
        
        # Crear HTML con audio autoplay
        audio_html = f'''
            <audio autoplay style="display:none">
                <source src="data:audio/mp3;base64,{audio_base64}" type="audio/mp3">
            </audio>
        '''
        return audio_html
    except Exception as e:
        print(f"Error generando audio: {e}")
        return None

def text_to_speech_thread(text, speed, word_queue, stop_flag):
    """Hilo de audio optimizado para Streamlit Cloud"""
    try:
        words = text.split()
        
        # Calcular delay basado en velocidad (WPM)
        word_delay = 60.0 / speed  # segundos por palabra
        
        for i, word in enumerate(words):
            if stop_flag[0]:
                print("Audio stopped by user")
                break
            
            # Enviar índice a la cola
            word_queue.put(i)
            
            # Generar y reproducir audio
            audio_html = play_audio_word(word)
            if audio_html:
                word_queue.put(f"AUDIO:{i}:{audio_html}")
            
            # Esperar según velocidad
            time.sleep(word_delay * 0.8)  # 80% del tiempo para permitir solapamiento
        
        word_queue.put(-1)  # Señal de finalización
        
    except Exception as e:
        print(f"Error en hilo de audio: {e}")
        word_queue.put(-1)

def text_to_speech_thread_fast(text, speed, word_queue, stop_flag):
    """Versión rápida para Streamlit Cloud"""
    try:
        words = text.split()
        
        # Determinar tamaño de chunk basado en velocidad
        if speed >= 500:
            chunk_size = 5
            chunk_delay = 0.25
        elif speed >= 400:
            chunk_size = 4
            chunk_delay = 0.2
        elif speed >= 300:
            chunk_size = 3
            chunk_delay = 0.15
        elif speed >= 200:
            chunk_size = 2
            chunk_delay = 0.1
        else:
            chunk_size = 1
            chunk_delay = 0.08
        
        for i in range(0, len(words), chunk_size):
            if stop_flag[0]:
                break
            
            end_idx = min(i + chunk_size, len(words))
            chunk_words = words[i:end_idx]
            chunk_text = ' '.join(chunk_words)
            
            # Enviar índices de todas las palabras del chunk
            for j in range(i, end_idx):
                word_queue.put(j)
            
            # Generar audio para el chunk
            audio_html = play_audio_word(chunk_text)
            if audio_html:
                word_queue.put(f"AUDIO:{i}:{audio_html}")
            
            time.sleep(chunk_delay)
        
        word_queue.put(-1)
        
    except Exception as e:
        print(f"Error: {e}")
        word_queue.put(-1)

def load_pages(pdf_path, start_page, end_page):
    """Carga todas las páginas en el rango seleccionado"""
    all_words_flat = []
    page_word_ranges = {}
    words_by_page = {}
    all_text = []
    
    # Barra de progreso
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    total_pages_range = end_page - start_page + 1
    
    for i, page_num in enumerate(range(start_page, end_page + 1)):
        status_text.text(f"Loading page {page_num}...")
        
        cache_key = f"{pdf_path}_{page_num}"
        if cache_key not in st.session_state.words_cache:
            words = extract_text_with_positions(pdf_path, page_num)
            words = [w for w in words if w[4].strip()]
            words.sort(key=lambda w: (w[1], w[0]))
            st.session_state.words_cache[cache_key] = words
        
        page_words = st.session_state.words_cache[cache_key]
        words_by_page[page_num] = page_words
        
        # Extraer texto para audio
        page_text = " ".join([w[4] for w in page_words])
        all_text.append(page_text)
        
        start_idx = len(all_words_flat)
        all_words_flat.extend(page_words)
        end_idx = len(all_words_flat)
        page_word_ranges[page_num] = (start_idx, end_idx)
        
        progress_bar.progress((i + 1) / total_pages_range)
    
    status_text.empty()
    progress_bar.empty()
    
    return all_words_flat, " ".join(all_text), page_word_ranges, words_by_page

def reset_pdf_state():
    """Resetea el estado cuando se carga un nuevo PDF"""
    st.session_state.words_cache = {}
    st.session_state.current_page = 1
    st.session_state.is_reading = False
    st.session_state.word_index = 0
    st.session_state.start_page = 1
    st.session_state.end_page = 1
    st.session_state.all_words_text = ""
    st.session_state.all_words_list = []
    st.session_state.page_word_ranges = {}
    st.session_state.words_by_page = {}
    st.session_state.stop_audio = True
    if 'stop_audio_ref' in st.session_state:
        st.session_state.stop_audio_ref[0] = True

# ======== UI ========

st.title("📖 PDF Audio Reader - Highlighting in PDF")

# Sección de carga de PDF
with st.container():
    st.subheader("📤 Load PDF")
    
    uploaded_file = st.file_uploader("Choose a PDF file", type="pdf", key="pdf_uploader")
    
    if uploaded_file is not None:
        # Guardar el archivo temporalmente
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, uploaded_file.name)
        
        # Verificar si es un archivo nuevo
        if st.session_state.pdf_path != temp_path:
            with open(temp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            # Resetear estado para nuevo PDF
            reset_pdf_state()
            st.session_state.pdf_path = temp_path
            st.session_state.pdf_name = uploaded_file.name
            
            # Obtener total de páginas
            doc = fitz.open(temp_path)
            st.session_state.total_pages = len(doc)
            st.session_state.end_page = min(3, len(doc))
            doc.close()
            
            st.success(f"✅ Loaded: {uploaded_file.name}")
            st.rerun()

# Verificar si hay un PDF cargado
if st.session_state.pdf_path is None:
    st.info("👆 Please upload a PDF file to begin")
    st.stop()

pdf_path = st.session_state.pdf_path
pdf_name = st.session_state.pdf_name
total_pages = st.session_state.total_pages

# Mostrar información del PDF actual
st.caption(f"📄 Current PDF: **{pdf_name}** ({total_pages} pages)")

# Configuración en sidebar
with st.sidebar:
    st.header("⚙️ Settings")
    
    if not st.session_state.is_reading:
        col1, col2 = st.columns(2)
        with col1:
            new_start_page = st.number_input("Start Page", 1, total_pages, st.session_state.start_page)
        with col2:
            new_end_page = st.number_input("End Page", new_start_page, total_pages, 
                                      max(st.session_state.end_page, new_start_page))
        
        # Detectar si cambió el rango de páginas
        if new_start_page != st.session_state.start_page or new_end_page != st.session_state.end_page:
            st.session_state.start_page = new_start_page
            st.session_state.end_page = new_end_page
            st.session_state.current_page = new_start_page
            # Forzar recarga de páginas
            st.session_state.all_words_list = []
            st.rerun()
    else:
        st.info(f"📖 Reading pages {st.session_state.start_page} to {st.session_state.end_page}")
    
    # Slider de velocidad
    reading_speed = st.slider("Reading Speed (WPM)", 100, 800, 250, 50,
                             help="Words per minute (higher = faster). 250+ for fast reading",
                             disabled=st.session_state.is_reading)
    
    # Opción de modo ultra rápido
    ultra_fast_mode = st.checkbox("🚀 Ultra Fast Mode", value=reading_speed > 400,
                                 help="Groups words for maximum speed",
                                 disabled=st.session_state.is_reading)
    
    # Opción para cambiar PDF
    if st.button("🔄 Change PDF"):
        reset_pdf_state()
        st.session_state.pdf_path = None
        st.rerun()
    
    st.markdown("---")
    st.markdown("### 📝 Instructions")
    st.markdown("""
    1. Upload a PDF file
    2. Select start and end pages
    3. Adjust speed
    4. Click START to begin
    5. Watch the PDF highlight in real-time
    """)

# Obtener páginas del estado
start_page = st.session_state.start_page
end_page = st.session_state.end_page

if start_page > end_page:
    st.error("Invalid page range.")
    st.stop()

# Cargar páginas si es necesario
if not st.session_state.all_words_list:
    with st.spinner("Loading pages..."):
        all_words_flat, all_text, page_word_ranges, words_by_page = load_pages(pdf_path, start_page, end_page)
        st.session_state.all_words_list = all_words_flat
        st.session_state.all_words_text = all_text
        st.session_state.page_word_ranges = page_word_ranges
        st.session_state.words_by_page = words_by_page

# Layout principal
col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("📄 PDF Viewer")
    pdf_placeholder = st.empty()
    
    # Controles de navegación
    nav_cols = st.columns([1, 1, 1, 1, 3])
    
    with nav_cols[0]:
        if st.button("⏮️ First", disabled=st.session_state.is_reading):
            st.session_state.current_page = start_page
            st.rerun()
    
    with nav_cols[1]:
        if st.button("◀ Prev", disabled=st.session_state.is_reading):
            new_page = max(start_page, st.session_state.current_page - 1)
            st.session_state.current_page = new_page
            st.rerun()
    
    with nav_cols[2]:
        if st.button("Next ▶", disabled=st.session_state.is_reading):
            new_page = min(end_page, st.session_state.current_page + 1)
            st.session_state.current_page = new_page
            st.rerun()
    
    with nav_cols[3]:
        if st.button("⏭️ Last", disabled=st.session_state.is_reading):
            st.session_state.current_page = end_page
            st.rerun()
    
    with nav_cols[4]:
        st.write(f"**Page {st.session_state.current_page} of {end_page}**")

with col2:
    st.subheader("🎮 Controls")
    
    current_page = st.session_state.current_page
    words_by_page = st.session_state.words_by_page
    
    if current_page in words_by_page:
        words_on_page = words_by_page[current_page]
        
        # Información
        col_stat1, col_stat2 = st.columns(2)
        with col_stat1:
            st.metric("Words/page", len(words_on_page))
        with col_stat2:
            st.metric("Total words", len(st.session_state.all_words_list))
        
        # Progreso actual
        if st.session_state.is_reading and st.session_state.all_words_list:
            progress = (st.session_state.word_index / len(st.session_state.all_words_list)) * 100
            st.metric("Progress", f"{progress:.1f}%")
        
        # Contenedor para audio
        audio_container = st.empty()
        
        # Botones de control
        col_btn1, col_btn2 = st.columns(2)
        
        with col_btn1:
            if not st.session_state.is_reading:
                if st.button("▶️ START", type="primary", key="start_btn", use_container_width=True):
                    # Limpiar cola
                    while not st.session_state.word_queue.empty():
                        try:
                            st.session_state.word_queue.get_nowait()
                        except Empty:
                            break
                    
                    # Resetear variables de control
                    st.session_state.stop_audio = False
                    st.session_state.is_reading = True
                    st.session_state.word_index = 0
                    st.session_state.current_page = start_page
                    
                    # Crear referencia mutable para el hilo
                    stop_audio_ref = [st.session_state.stop_audio]
                    
                    # Elegir versión según modo
                    if ultra_fast_mode:
                        thread_func = text_to_speech_thread_fast
                    else:
                        thread_func = text_to_speech_thread
                    
                    # Iniciar hilo de audio
                    audio_thread = threading.Thread(
                        target=thread_func,
                        args=(st.session_state.all_words_text, reading_speed, 
                              st.session_state.word_queue, stop_audio_ref),
                        daemon=True
                    )
                    audio_thread.start()
                    st.session_state.audio_thread = audio_thread
                    st.session_state.stop_audio_ref = stop_audio_ref
                    
                    st.rerun()
        
        with col_btn2:
            if st.session_state.is_reading:
                if st.button("⏹️ STOP", type="secondary", key="stop_btn", use_container_width=True):
                    # Activar bandera de stop
                    st.session_state.stop_audio = True
                    if 'stop_audio_ref' in st.session_state:
                        st.session_state.stop_audio_ref[0] = True
                    
                    # Esperar un momento
                    time.sleep(0.5)
                    
                    st.session_state.is_reading = False
                    st.rerun()
        
        # Procesar cola de palabras
        if st.session_state.is_reading:
            try:
                messages_processed = 0
                while not st.session_state.word_queue.empty() and messages_processed < 30:
                    message = st.session_state.word_queue.get_nowait()
                    messages_processed += 1
                    
                    if message == -1:  # Finalización
                        st.session_state.is_reading = False
                        st.balloons()
                        st.success("✅ Reading completed!")
                        st.rerun()
                    elif isinstance(message, int):  # Índice de palabra
                        word_idx = message
                        if word_idx < len(st.session_state.all_words_list):
                            st.session_state.word_index = word_idx
                            
                            # Actualizar página
                            for page_num, (start_idx, end_idx) in st.session_state.page_word_ranges.items():
                                if start_idx <= word_idx < end_idx:
                                    if st.session_state.current_page != page_num:
                                        st.session_state.current_page = page_num
                                    break
                    elif isinstance(message, str) and message.startswith("AUDIO:"):
                        # Mensaje de audio
                        parts = message.split(":", 2)
                        if len(parts) == 3:
                            audio_html = parts[2]
                            audio_container.markdown(audio_html, unsafe_allow_html=True)
            except Empty:
                pass
        
        # Barra de progreso y palabra actual
        if st.session_state.all_words_list:
            progress = st.session_state.word_index / len(st.session_state.all_words_list)
            st.progress(progress)
            
            # Palabra actual
            if st.session_state.word_index < len(st.session_state.all_words_list):
                current_word = st.session_state.all_words_list[st.session_state.word_index][4]
                st.markdown("### 📢 Now reading:")
                
                st.markdown(f"""
                <div style='
                    text-align: center;
                    padding: 20px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    border-radius: 10px;
                    margin: 10px 0;
                '>
                    <h1 style='
                        color: white;
                        font-size: 48px;
                        margin: 0;
                        text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
                    '>{current_word}</h1>
                </div>
                """, unsafe_allow_html=True)

# ===== MOSTRAR PDF CON RESALTADOS =====
current_display_page = st.session_state.current_page
words_by_page = st.session_state.words_by_page

if current_display_page in words_by_page:
    if st.session_state.is_reading:
        # Calcular palabras a resaltar
        page_start_idx, _ = st.session_state.page_word_ranges[current_display_page]
        word_index = st.session_state.word_index
        relative_idx = word_index - page_start_idx
        
        if relative_idx >= 0:
            words_to_highlight = list(range(min(relative_idx + 1, len(words_by_page[current_display_page]))))
            
            try:
                highlighted_img = highlight_words_on_page(
                    pdf_path,
                    current_display_page,
                    words_to_highlight,
                    words_by_page[current_display_page]
                )
                pdf_placeholder.image(highlighted_img, use_container_width=True)
            except Exception as e:
                st.error(f"Error highlighting: {e}")
                # Fallback
                doc = fitz.open(pdf_path)
                page = doc[current_display_page - 1]
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                pdf_placeholder.image(pix.tobytes("png"), use_container_width=True)
                doc.close()
        else:
            doc = fitz.open(pdf_path)
            page = doc[current_display_page - 1]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            pdf_placeholder.image(pix.tobytes("png"), use_container_width=True)
            doc.close()
    else:
        # Mostrar página sin resaltar
        doc = fitz.open(pdf_path)
        page = doc[current_display_page - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        pdf_placeholder.image(pix.tobytes("png"), use_container_width=True)
        doc.close()

# Auto-refresh durante la lectura
if st.session_state.is_reading:
    time.sleep(0.1)
    st.rerun()

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: gray; padding: 10px;'>
    PDF Audio Reader - Optimized for Streamlit Cloud<br>
    Made with ❤️ using gTTS + HTML5 Audio
</div>
""", unsafe_allow_html=True)
