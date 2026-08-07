from __future__ import annotations

import textwrap

import streamlit as st

# Palette pulled from propertypie.com.au (logo mark + hero gradient)
BLUEBERRY_PURPLE = "#2E0A76"
PLUM_PURPLE = "#831B98"
MID_PURPLE = "#8A2EB8"
BERRY_ORANGE = "#F97A1F"
DEEP_ORANGE = "#EC4913"

GRADIENT = f"linear-gradient(135deg, {BLUEBERRY_PURPLE}, {PLUM_PURPLE}, {MID_PURPLE}, {BERRY_ORANGE}, {DEEP_ORANGE})"

LOGO_SVG = f"""
<svg width="34" height="34" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
  <circle cx="12" cy="12" r="10" fill="{BERRY_ORANGE}"/>
  <path d="M12 2 A10 10 0 0 1 20.66 8 L12 12 Z" fill="{PLUM_PURPLE}"/>
  <path d="M12 2 A10 10 0 0 1 16 21.86 L12 12 Z" fill="{BLUEBERRY_PURPLE}"/>
</svg>
"""


def inject_branding() -> None:
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        html, body, [class*="css"] {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }}

        h1, h2, h3 {{
            color: {BLUEBERRY_PURPLE};
        }}

        [data-testid="stSidebar"] {{
            background: linear-gradient(180deg, {BLUEBERRY_PURPLE} 0%, {PLUM_PURPLE} 100%);
        }}
        [data-testid="stSidebar"] * {{
            color: #FFFFFF !important;
        }}
        [data-testid="stSidebar"] a:hover {{
            color: {BERRY_ORANGE} !important;
        }}

        button[kind*="primary"] {{
            background: {GRADIENT} !important;
            border: none !important;
            color: white !important;
        }}
        button[kind*="primary"]:hover {{
            filter: brightness(1.08);
        }}

        [data-testid="stMetricValue"] {{
            color: {BLUEBERRY_PURPLE};
        }}

        /* Touch-friendly tap targets on small screens */
        @media (max-width: 480px) {{
            .brand-header {{
                flex-wrap: wrap;
                row-gap: 2px;
            }}
            .brand-header .brand-page-title {{
                width: 100%;
                margin-left: 44px; /* aligns under the wordmark, below the logo+wordmark line */
            }}
            button {{
                min-height: 2.75rem;
            }}
            [data-testid="stFileUploaderDropzone"] {{
                padding: 0.75rem !important;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def brand_header(page_title: str, subtitle: str | None = None) -> None:
    logo_inline = " ".join(line.strip() for line in LOGO_SVG.strip().splitlines())
    html = f"""<div class="brand-header" style="display:flex;align-items:center;gap:10px;margin-bottom:0.25rem;">
{logo_inline}
<span style="font-size:1.6rem;font-weight:700;white-space:nowrap;"><span style="color:{BLUEBERRY_PURPLE};">quote</span><span style="color:{BERRY_ORANGE};">sourcing</span></span>
<span class="brand-page-title" style="color:#94a3b8;font-weight:400;font-size:1.1rem;"> &middot; {page_title}</span>
</div>"""
    st.markdown(textwrap.dedent(html), unsafe_allow_html=True)
    if subtitle:
        st.caption(subtitle)
