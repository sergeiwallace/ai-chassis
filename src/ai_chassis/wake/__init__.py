"""Event-driven host dispatch, independent of any particular message system.

Shipped subclasses derive directly from one framework base. Share behavior with
helpers, not intermediate inheritance layers. No source may sample for changes.
The notifier selects this whole path before entering any legacy scheduling.
"""
