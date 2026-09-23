from hikari.impl.special_endpoints import ContainerComponentBuilder

COLORS = {
    "SUCCESS": 0x2ECC71,
    "ERROR": 0xE74C3C,
    "WARNING": 0xF1C40F,
    "INFO": 0x3498DB,
    "DEFAULT": 0x2B2D31
}

def _build_container_with_section(title: str, message: str, color: int) -> ContainerComponentBuilder:
    c = ContainerComponentBuilder(id=1, accent_color=color)
    c.add_text_display(title)
    c.add_text_display(message)
    return c

def create_error(message: str) -> ContainerComponentBuilder:
    return _build_container_with_section("# 🚫 Error", message, COLORS["ERROR"])

def create_success(message: str, title: str = "Success") -> ContainerComponentBuilder:
    return _build_container_with_section(f"# ✅ {title}", message, COLORS["SUCCESS"])

def create_info(message: str, title: str = "Info") -> ContainerComponentBuilder:
    return _build_container_with_section(f"# <:important:1545674807411089418> {title}", message, COLORS["INFO"])

def create_warning(message: str, title: str = "Warning") -> ContainerComponentBuilder:
    return _build_container_with_section(f"# ⚠️ {title}", message, COLORS["WARNING"])

def sanitize_mention(text: str) -> str:
    """H3: prevent @everyone/@here and role/user mention spam in Components V2."""
    if not text:
        return text
    # Break @everyone/@here, escape <@ ...> mentions by inserting zero-width space
    return text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere").replace("<@", "<\u200b@")

def sanitize_manga_name(name: str) -> str:
    """H3/M5: sanitize and truncate manga name for display (also prevents Drive ID leak if misused)."""
    if not name:
        return name
    # Limit length and sanitize mentions
    name = name.strip()[:100]
    return sanitize_mention(name)

