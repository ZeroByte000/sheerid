#!/usr/bin/env python3
"""
SheerID Multi-Verifier - ORIGINAL LOGIC EDITION
Disesuaikan dengan fungsi asli (sheerid_verifier.py) + Dynamic Program ID fix.
"""

import re
import random
import argparse
import sys
import os
from typing import Dict, Optional, Tuple
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table
from rich.markdown import Markdown
from datetime import datetime

# ==========================================
# 1. CONFIG & DATA (imported from k12)
# ==========================================
from k12.config import PROGRAM_ID as DEFAULT_PROGRAM_ID, SHEERID_BASE_URL, MY_SHEERID_URL, SCHOOLS, DEFAULT_SCHOOL_ID

# Keep military fallback values (used as fallback in some flows)
MILITARY_BRANCHES = ['ARMY', 'NAVY', 'AIR_FORCE', 'MARINES', 'COAST_GUARD', 'SPACE_FORCE']
MILITARY_STATUSES = ['ACTIVE_DUTY', 'RESERVIST', 'VETERAN', 'RETIREE']

# ==========================================
# 2. NAME GENERATOR (imported from k12)
# ==========================================
from k12.name_generator import NameGenerator, generate_email, generate_birth_date

# ==========================================
# 3. DOC GENERATOR (use k12 implementations)
# ==========================================
from k12.img_generator import generate_teacher_pdf, generate_teacher_png

# ==========================================
# 4. MAIN VERIFIER LOGIC
# ==========================================
# Use upstream K12 verifier implementation
from k12.sheerid_verifier import SheerIDVerifier as K12Verifier

# K12Verifier provides: parse_verification_id, verify(...) and returns a dict similar to original implementation


# ==========================================
# 5. CLI INTERFACE (Neon Cyan)
# ==========================================

console = Console()
CYAN_STYLE = "cyan"
BOLD_CYAN = "bold cyan"

def tampilkan_banner():
    text = """
🚀 SHEERID VERIFIER - ORIGINAL LOGIC EDITION

✨ Credit: ZeroByte
    """
    banner = Panel(
        text, 
        title="🎓", 
        subtitle="ZeroByte • Ultimate 2026", 
        border_style="cyan",
        style=CYAN_STYLE
    )
    console.print(banner)

def main():
    parser = argparse.ArgumentParser(
        description='SheerID Verifier - Original Logic',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Contoh: python main.py --url 'https://...'"
    )
    parser.add_argument('verification_id', nargs='?', help='ID verifikasi atau URL')
    parser.add_argument('--url', help='URL verifikasi lengkap')
    parser.add_argument('--nama', help='Nama lengkap')
    parser.add_argument('--email', help='Email custom')
    parser.add_argument('--resend-email', action='store_true', help='Automatically resend verification email and wait for docUpload')
    parser.add_argument('--sekolah', choices=SCHOOLS.keys(), default=DEFAULT_SCHOOL_ID, help='ID sekolah')
    
    args = parser.parse_args()
    
    os.system("cls" if os.name == "nt" else "clear")
    tampilkan_banner()
    
    verification_id_input = args.verification_id or args.url
    if not verification_id_input:
        verification_id_input = input("Masukkan verification ID atau URL: ").strip()
    if not verification_id_input:
        console.print("Masukkan verification ID atau URL!", style=BOLD_CYAN)
        sys.exit(1)

    if not args.nama:
        nama_input = input("Nama lengkap (opsional, Enter untuk skip): ").strip()
        if nama_input:
            args.nama = nama_input

    if not args.email:
        email_input = input("Email custom (opsional, Enter untuk skip): ").strip()
        if email_input:
            args.email = email_input

    if not args.resend_email:
        resend_input = input("Resend email verifikasi? [y/N]: ").strip().lower()
        if resend_input in ("y", "yes"):
            args.resend_email = True
    

    # Parse Verification ID
    if args.url or '/' in verification_id_input or verification_id_input.startswith('http'):
        verification_id = K12Verifier.parse_verification_id(verification_id_input)
    else:
        verification_id = verification_id_input.strip().lower()
    
    if not verification_id:
        console.print("Invalid verification ID!", style=BOLD_CYAN)
        sys.exit(1)

    console.print(f"Target ID: {verification_id}", style=BOLD_CYAN)
    console.print()
    
    try:
        nama_parts = args.nama.split(' ', 1) if args.nama else None
        nama_depan = nama_parts[0] if nama_parts else None
        nama_belakang = nama_parts[1] if nama_parts and len(nama_parts) > 1 else None

        # Use K12 verifier
        verifier = K12Verifier(verification_id)
        result = verifier.verify(nama_depan, nama_belakang, args.email, None, args.sekolah, args.resend_email)

        if result['success']:
            redirect_url = result.get('redirect_url') or f"{MY_SHEERID_URL}/verifications/{verification_id}"
            success_panel = Panel.fit(
                Markdown(f"""
# VERIFIKASI BERHASIL!

✅ Dokumen Terkirim
✅ Menunggu Review

Detail:
• ID: `{verification_id}`
• Link: {redirect_url}
                """), 
                title="SUCCESS",
                border_style="cyan",
                style=CYAN_STYLE
            )
            console.print(success_panel)
        else:
            raise Exception(result.get('message', 'Unknown Error'))
        
    except KeyboardInterrupt:
        console.print("Dibatalkan", style=CYAN_STYLE)
    except Exception as e:
        error_panel = Panel(
            f"VERIFIKASI GAGAL\n\n{str(e)}",
            title="Error", 
            border_style="cyan", 
            style=CYAN_STYLE
        )
        console.print(error_panel)
        sys.exit(1)

if __name__ == "__main__":
    try:
        import rich
        # xhtml2pdf is required for K12 PDF rendering; Playwright/PIL are optional for PNG
        import xhtml2pdf
    except ImportError as e:
        print(f"Missing library: {e}")
        sys.exit(1)

    main()
