
import re

from PyQt6.QtCore import QByteArray, QEvent, QObject, QSize
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QAbstractButton, QApplication, QLabel, QListWidget, QMenu, QToolBar, QToolButton


__all__ = ['QtDynamicProperty', 'ContextMenuActions', 'is_dark_palette', 'palette_icon', 'recolored_svg_pixmaps', 'adapted_icon', 'IconPaletteAdapter']


class QtDynamicProperty(object):
    def __init__(self, name, type=str):
        self.name = name
        self.type = type

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.property(self.name)

    def __set__(self, obj, value):
        if value is not None and not isinstance(value, self.type):
            value = self.type(value)
        obj.setProperty(self.name, value)

    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


class ContextMenuActions(object):
    pass


def is_dark_palette(palette):
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


_palette_icon_cache = {}


def palette_icon(icon, palette=None, role=QPalette.ColorRole.WindowText):
    """Return the icon recolored with the palette color for role if the palette is dark, as the dark monochrome icons are unreadable on dark backgrounds"""
    palette = palette or QApplication.palette()
    if icon.isNull() or not is_dark_palette(palette):
        return icon
    color = palette.color(role)
    key = icon.cacheKey(), color.rgba()
    try:
        return _palette_icon_cache[key]
    except KeyError:
        pass
    tinted_icon = QIcon()
    for size in icon.availableSizes() or [QSize(extent, extent) for extent in (16, 32, 64, 128)]:
        pixmap = icon.pixmap(size)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), color)
        painter.end()
        tinted_icon.addPixmap(pixmap)
    _palette_icon_cache[key] = tinted_icon
    return tinted_icon


def recolored_svg_pixmaps(filename, replacements, extents=(16, 32, 64, 128)):
    """Render an SVG file after applying the {old: new} text replacements to it (e.g. to change the colors in its styles)"""
    with open(filename) as svg_file:
        data = svg_file.read()
    for old, new in replacements.items():
        data = data.replace(old, new)
    renderer = QSvgRenderer(QByteArray(data.encode()))
    pixmaps = []
    for extent in extents:
        pixmap = QPixmap(extent, extent)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmaps.append(pixmap)
    return pixmaps




_dark_monochrome_cache = {}
_lightened_icon_cache = {}


def _is_dark_monochrome(pixmap):
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    count = luminance = saturation = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() < 160:
                continue
            count += 1
            luminance += 0.2126*color.red() + 0.7152*color.green() + 0.0722*color.blue()
            saturation += color.hsvSaturationF()
    return count > 0 and saturation / count < 0.2 and luminance / count < 110


def is_dark_monochrome_icon(icon, state=QIcon.State.Off):
    key = icon.cacheKey(), state
    try:
        return _dark_monochrome_cache[key]
    except KeyError:
        return _dark_monochrome_cache.setdefault(key, not icon.isNull() and _is_dark_monochrome(icon.pixmap(QSize(32, 32), QIcon.Mode.Normal, state)))


def lightened_icon(icon):
    """Return the icon with the gray levels of its dark monochrome states inverted, which keeps their details (unlike tinting)"""
    try:
        return _lightened_icon_cache[icon.cacheKey()]
    except KeyError:
        pass
    new_icon = QIcon()
    for state in (QIcon.State.Off, QIcon.State.On):
        invert = is_dark_monochrome_icon(icon, state)
        for size in icon.availableSizes(QIcon.Mode.Normal, state) or [QSize(extent, extent) for extent in (16, 32, 64, 128)]:
            pixmap = icon.pixmap(size, QIcon.Mode.Normal, state)
            if invert:
                image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
                image.invertPixels(QImage.InvertMode.InvertRgb)
                pixmap = QPixmap.fromImage(image)
            new_icon.addPixmap(pixmap, QIcon.Mode.Normal, state)
    _lightened_icon_cache[icon.cacheKey()] = new_icon
    _lightened_icon_cache[new_icon.cacheKey()] = new_icon  # already lightened
    return new_icon


def adapted_icon(icon, palette=None):
    """Return the icon to use with the palette: a lightened version of a dark monochrome icon when the palette is dark"""
    palette = palette or QApplication.palette()
    if icon.isNull() or not is_dark_palette(palette):
        return icon
    if not (is_dark_monochrome_icon(icon, QIcon.State.Off) or is_dark_monochrome_icon(icon, QIcon.State.On)):
        return icon
    return lightened_icon(icon)


_light_background_re = re.compile(r'background(?:-color)?\s*:\s*(white|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\))')


def _is_light_css_color(value):
    if value == 'white':
        return True
    color = QColor(value.replace(' ', '')) if value.startswith('#') else QColor(*map(int, re.findall(r'\d+', value)))
    return color.isValid() and color.lightness() > 180


def on_light_background(widget):
    """Tell if the widget is drawn on a background that stays light regardless of the palette"""
    while widget is not None:
        if widget.property('lightBackground'):
            return True
        match = _light_background_re.search(widget.styleSheet())
        if match and _is_light_css_color(match.group(1)):
            return True
        widget = widget.parentWidget()
    return False


class IconPaletteAdapter(QObject):
    """
    Application wide event filter that lightens the dark monochrome icons of
    widgets, menus and tool bars when the palette is dark (and restores them
    when it becomes light again), except on backgrounds that remain light.
    Widgets that paint their own light background can opt out by setting the
    lightBackground property (which also covers their children), and widgets
    showing pictures (e.g. user icons) by setting the keepIconColors property.
    Label pixmaps are only considered up to icon sizes, to leave pictures alone.
    """

    max_label_icon_size = 24

    def __init__(self, parent=None):
        super(IconPaletteAdapter, self).__init__(parent)
        self.dark = is_dark_palette(QApplication.palette())

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type == QEvent.Type.Polish:
            self._adapt_widget(watched)
        elif event_type in (QEvent.Type.ActionAdded, QEvent.Type.ActionChanged) and isinstance(watched, (QMenu, QToolBar)):
            self._adapt_action(event.action(), watched)
        elif event_type == QEvent.Type.ApplicationPaletteChange and watched is QApplication.instance():
            dark = is_dark_palette(QApplication.palette())
            if dark != self.dark:
                self.dark = dark
                for widget in QApplication.allWidgets():
                    self._adapt_widget(widget)
        return False

    def _adapted(self, obj, icon):
        original = obj.property('originalIcon')
        if original is not None and icon.cacheKey() == obj.property('adaptedIconKey'):
            icon = original  # the icon we set earlier, start again from the original one
        new_icon = adapted_icon(icon) if self.dark else icon
        obj.setProperty('originalIcon', icon)
        obj.setProperty('adaptedIconKey', new_icon.cacheKey())
        return new_icon

    def _adapt_action(self, action, container):
        if action is None or action.icon().isNull() or on_light_background(container):
            return
        new_icon = self._adapted(action, action.icon())
        if new_icon.cacheKey() != action.icon().cacheKey():
            action.setIcon(new_icon)

    def _adapt_widget(self, widget):
        if widget.property('keepIconColors'):
            return
        if isinstance(widget, (QMenu, QToolBar)):
            for action in widget.actions():
                self._adapt_action(action, widget)
        elif isinstance(widget, QAbstractButton) and not (isinstance(widget, QToolButton) and widget.defaultAction() is not None):
            if not widget.icon().isNull() and not on_light_background(widget):
                new_icon = self._adapted(widget, widget.icon())
                if new_icon.cacheKey() != widget.icon().cacheKey():
                    widget.setIcon(new_icon)
        elif isinstance(widget, QLabel):
            pixmap = widget.pixmap()
            if pixmap is None or pixmap.isNull():
                return
            if pixmap.cacheKey() == widget.property('adaptedPixmapKey'):
                pixmap = widget.property('originalPixmap')  # the pixmap we set earlier, start again from the original one
            size = pixmap.deviceIndependentSize()
            if max(size.width(), size.height()) > self.max_label_icon_size or on_light_background(widget):
                return
            icon = QIcon(pixmap)
            new_pixmap = adapted_icon(icon).pixmap(size.toSize(), pixmap.devicePixelRatio()) if self.dark and is_dark_monochrome_icon(icon) else pixmap
            widget.setProperty('originalPixmap', pixmap)
            widget.setProperty('adaptedPixmapKey', new_pixmap.cacheKey())
            if new_pixmap.cacheKey() != widget.pixmap().cacheKey():
                widget.setPixmap(new_pixmap)
        elif isinstance(widget, QListWidget) and not on_light_background(widget):
            for row in range(widget.count()):
                item = widget.item(row)
                if not item.icon().isNull():
                    item.setIcon(adapted_icon(item.icon()) if self.dark else item.icon())
