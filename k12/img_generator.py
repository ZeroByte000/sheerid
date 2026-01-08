"""K12 doc generator: uses card-temp.html to render PDF (xhtml2pdf) and PNG (Playwright).
"""
import random
from datetime import datetime
from io import BytesIO
from pathlib import Path

from xhtml2pdf import pisa


def _render_template(first_name: str, last_name: str):
    full_name = f"{first_name} {last_name}"
    employee_id = random.randint(1000000, 9999999)
    current_date = datetime.now().strftime("%m/%d/%Y %I:%M %p")

    template_path = Path(__file__).parent / "card-temp.html"
    html = template_path.read_text(encoding="utf-8")

    color_map = {
        "var(--primary-blue)": "#0056b3",
        "var(--border-gray)": "#dee2e6",
        "var(--bg-gray)": "#f8f9fa",
    }
    for placeholder, color in color_map.items():
        html = html.replace(placeholder, color)

    html = html.replace("Sarah J. Connor", full_name)
    html = html.replace("E-9928104", f"E-{employee_id}")
    html = html.replace('id="currentDate"></span>', f'id="currentDate">{current_date}</span>')

    # Return html plus metadata (employee id and date) so callers can persist files
    return html, employee_id, current_date


def generate_teacher_pdf(first_name: str, last_name: str) -> bytes:
    html, employee_id, current_date = _render_template(first_name, last_name)
    output = BytesIO()
    pisa_status = pisa.CreatePDF(html, dest=output, encoding="utf-8")
    if pisa_status.err:
        raise Exception("PDF generation failed")
    pdf_data = output.getvalue()
    output.close()
    return pdf_data


def generate_teacher_png(first_name: str, last_name: str) -> bytes:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is required for PNG generation. Run `pip install playwright` and `playwright install chromium`") from exc

    html, employee_id, current_date = _render_template(first_name, last_name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 1000})
        page.set_content(html, wait_until="load")
        page.wait_for_timeout(500)
        card = page.locator('.browser-mockup')
        png_bytes = card.screenshot(type='png')
        browser.close()

    # Save PNG to project-level image/ directory
    img_dir = Path(__file__).parent.parent / "image"
    img_dir.mkdir(parents=True, exist_ok=True)

    # Use a fixed filename to make cleanup easy
    img_path = img_dir / "data.png"
    # Remove existing file if present
    try:
        if img_path.exists():
            img_path.unlink()
    except Exception:
        pass

    with open(img_path, 'wb') as f:
        f.write(png_bytes)

    # Return bytes as before
    return png_bytes
