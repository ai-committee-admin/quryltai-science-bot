"""Bounded document extraction in an isolated subprocess; no OCR or macros."""
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 60000


def extract(data, suffix):
    if len(data) > MAX_BYTES:
        raise ValueError('файл больше 10 МБ')
    parts, warnings = [], []
    if suffix == '.docx':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 30 * 1024 * 1024:
                raise ValueError('слишком большой распакованный Word')
            xml = archive.read('word/document.xml')
            if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:
                raise ValueError('неподдерживаемая структура XML')
            root = ElementTree.fromstring(xml)
            ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            for p in root.iter(ns + 'p'):
                text = ''.join(n.text or '' for n in p.iter(ns + 't')).strip()
                if text:
                    parts.append(text)
            warnings.append('Word: извлечён основной текст и текст таблиц; изображения, колонтитулы и примечания не анализировались.')
    elif suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError('PDF защищён паролем')
        if len(reader.pages) > 100:
            raise ValueError('PDF содержит больше 100 страниц')
        blank = []
        for i, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or '').strip()
            if text:
                parts.append(f'Страница {i}:\n{text}')
            else:
                blank.append(str(i))
            if sum(map(len, parts)) > MAX_CHARS:
                raise ValueError('текст файла превышает 60 000 символов')
        if blank:
            warnings.append('PDF: нет извлекаемого текста на страницах ' + ', '.join(blank) + '; возможны сканы, OCR не выполнялся.')
        warnings.append('PDF: анализируется текстовый слой; изображения и точность порядка ячеек таблиц не проверены.')
    else:
        raise ValueError('поддерживаются PDF и Word .docx; старый .doc сохраните как .docx')
    text = '\n'.join(parts)
    if not text:
        raise ValueError('нет извлекаемого текста; для скана нужен OCR или текстовая версия')
    if len(text) > MAX_CHARS:
        raise ValueError('текст файла превышает 60 000 символов')
    return {'text': text, 'warnings': warnings}


def extract_document(data, name):
    suffix = Path(name).suffix.lower()
    if suffix not in ('.pdf', '.docx'):
        raise ValueError('поддерживаются PDF и Word .docx; старый .doc сохраните как .docx')
    if len(data) > MAX_BYTES:
        raise ValueError('файл больше 10 МБ')
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), suffix],
                                input=data, capture_output=True, timeout=25)
        if result.returncode:
            raise ValueError('не удалось прочитать файл: повреждение или превышение ресурсов')
        parsed = json.loads(result.stdout)
        if 'error' in parsed:
            raise ValueError(parsed['error'])
        return parsed
    except subprocess.TimeoutExpired:
        raise ValueError('превышено время чтения файла (25 секунд)') from None


if __name__ == '__main__':
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        result = extract(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1])
    except ValueError as exc:
        result = {'error': str(exc)}
    except Exception:
        result = {'error': 'не удалось прочитать файл; проверьте формат и целостность'}
    print(json.dumps(result, ensure_ascii=False))
