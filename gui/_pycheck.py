import ast, sys
for fn in ("backend.py", "gui_main.py"):
    try:
        ast.parse(open(fn, encoding="utf-8").read())
        print("OK", fn)
    except SyntaxError as e:
        print("SYNTAX_ERR", fn, e)
        sys.exit(1)
print("PY_AST_OK")
