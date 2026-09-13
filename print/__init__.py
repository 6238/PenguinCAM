"""The 3D printing path: slicer wrapper, job pool, Flask blueprint and assets.

Self-contained by design, so another fabrication technology can sit beside it in
its own top-level directory. See docs/3D_PRINTING.md.

Never write `import print` — that binds the name `print` in the importing module
and shadows the builtin. Use `from print import jobs` or `from print.routes import
init_print_routes`.
"""
