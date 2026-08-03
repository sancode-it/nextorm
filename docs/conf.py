"""Sphinx configuration for NextORM documentation."""

import sys
from importlib import metadata
from pathlib import Path

# Make the nextorm package importable without installation
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Project info
# ---------------------------------------------------------------------------

project = "NextORM"
copyright = "2026, Henri Hulski"
author = "Henri Hulski"


# The version info for the project you're documenting, acts as replacement for
# |version| and |release|, also used in various other places throughout the
# built documents.
#
# The short X.Y version.
try:
    version = metadata.version("nextorm")
except metadata.PackageNotFoundError:
    # Fallback for ReadTheDocs and other environments where the package isn't installed
    # Try to get version from pyproject.toml
    import os
    import re

    try:
        pyproject_path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(pyproject_path) as f:
            content = f.read()
        # Simple regex to extract version
        version_match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
        version = version_match.group(1) if version_match else "0.0.0"
    except (FileNotFoundError, Exception):
        version = "0.0.0"  # fallback for un-installed dev builds

    release = version


# ---------------------------------------------------------------------------
# General Sphinx configuration
# ---------------------------------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "sphinx_copybutton",
    "sphinx_design",
]

templates_path = ["_templates"]
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "TODO.rst",
    "migration_from_ponyorm.rst",
    "ponyorm_comparison.rst",
]

# ---------------------------------------------------------------------------
# HTML output — Furo theme
# ---------------------------------------------------------------------------

html_theme = "furo"
html_title = "NextORM"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_favicon = "_static/icon-64.png"

html_theme_options = {
    "light_logo": "logo-light.svg",
    "dark_logo": "logo-dark.svg",
    "sidebar_hide_name": True,
    "navigation_with_keys": True,
    "source_repository": "https://github.com/sancode-it/nextorm",
    "source_branch": "main",
    "source_directory": "docs/",
    "dark_css_variables": {
        "color-brand-primary": "#0095c4",
        "color-brand-content": "#48c9e8",
        "color-brand-visited": "#6aaad4",
        "color-highlighted-background": "#6b3d0a",
        "color-highlight-on-target": "#1a3a45",
    },
    "footer_icons": [
        {
            "name": "Discord",
            "url": "https://discord.gg/<INVITE_CODE>",
            "html": """
                <svg stroke="currentColor" fill="currentColor" stroke-width="0"
                    viewBox="0 0 24 24">
                    <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074
                        0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0
                        0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0
                        0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0
                        0-.032.027C.533 9.046-.32 13.58.099 18.057c.002.022.012.043
                        .03.056a19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0
                        .084-.028 14.09 14.09 0 0 0 1.226-1.994.076.076 0 0
                        0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0
                        1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1
                        .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1
                        .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127
                        12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36
                        .698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839
                        19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177
                        -.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03z"/>
                </svg>
            """,
            "class": "",
        },
        {
            "name": "GitHub",
            "url": "https://github.com/sancode-it/nextorm",
            "html": """
                <svg stroke="currentColor" fill="currentColor" stroke-width="0"
                    viewBox="0 0 16 16">
                    <path fill-rule="evenodd"
                        d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38
                        0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13
                        -.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66
                        .07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15
                        -.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0
                        1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82
                        1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01
                        1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z">
                    </path>
                </svg>
            """,
            "class": "",
        },
    ],
}

# ---------------------------------------------------------------------------
# Autodoc settings
# ---------------------------------------------------------------------------

autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
    "special-members": "__init__",
    "exclude-members": "__weakref__, __dict__, __doc__, __module__",
}

autodoc_typehints = "description"
autodoc_typehints_format = "short"
autodoc_class_signature = "separated"

# ---------------------------------------------------------------------------
# Napoleon (NumPy / Google-style docstrings)
# ---------------------------------------------------------------------------

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = False
napoleon_use_rtype = True

# ---------------------------------------------------------------------------
# Intersphinx — link to Python stdlib docs
# ---------------------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# ---------------------------------------------------------------------------
# Autosummary
# ---------------------------------------------------------------------------

autosummary_generate = True

# ---------------------------------------------------------------------------
# Copy-button — skip prompts and output lines
# ---------------------------------------------------------------------------

copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True

# ---------------------------------------------------------------------------
# Pygments — syntax highlighting
# ---------------------------------------------------------------------------

pygments_dark_style = "lightbulb"
