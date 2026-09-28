"""Exercise real startup PORT expressions without starting servers or databases."""
import ast
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class PortConfigTests(unittest.TestCase):
    def test_startup_ports(self):
        for filename in ("main.py", "mcp_server.py", "character_gateway.py"):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            expressions = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "int"
                and any(isinstance(child, ast.Constant) and child.value == "PORT"
                        for child in ast.walk(node))
            ]
            self.assertEqual(len(expressions), 1, filename)
            code = compile(ast.Expression(expressions[0]), filename, "eval")
            for value, expected in ((None, 8080), ("", 8080), ("  \t", 8080),
                                    ("8080", 8080), ("31234", 31234), (" 9000 ", 9000)):
                with self.subTest(file=filename, value=value), patch.dict(os.environ):
                    os.environ.pop("PORT", None)
                    if value is not None:
                        os.environ["PORT"] = value
                    self.assertEqual(eval(code, {"os": os}), expected)
            with self.subTest(file=filename, value="invalid"), patch.dict(os.environ, PORT="invalid"):
                with self.assertRaises(ValueError):
                    eval(code, {"os": os})


if __name__ == "__main__":
    unittest.main()
