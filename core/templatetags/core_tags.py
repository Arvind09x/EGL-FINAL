"""Custom template tags and filters for the core app."""

import os

from django import template
from django.conf import settings
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def is_reserved(reserved_pairs, copy_id, slot_id):
    """Check if a (copy_id, slot_id) pair is in the reserved set."""
    return (copy_id, slot_id) in reserved_pairs


@register.simple_tag
def game_image_url(game_name):
    """
    Resolve a game image from static/images/games/ based on the game name.
    Converts the name to lowercase, replaces spaces with underscores, and
    checks for common image extensions (.png, .jpg, .jpeg, .webp).
    Returns the static URL if found, or empty string otherwise.
    """
    slug = game_name.strip().lower().replace(" ", "_").replace("-", "_")
    games_dir = os.path.join(settings.BASE_DIR, "static", "images", "games")

    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        filename = f"{slug}{ext}"
        filepath = os.path.join(games_dir, filename)
        if os.path.isfile(filepath):
            return static(f"images/games/{filename}")

    return ""


@register.simple_tag
def logo_static_url():
    """Return the static URL for the Eeshto logo."""
    logo_dir = os.path.join(settings.BASE_DIR, "static", "images", "logo")
    for ext in (".png", ".svg", ".jpg", ".webp"):
        filename = f"logo{ext}"
        if os.path.isfile(os.path.join(logo_dir, filename)):
            return static(f"images/logo/{filename}")
    return ""
