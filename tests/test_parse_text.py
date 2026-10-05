import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.parse_text as parse_text_module
import src.tools.process_variables.main as process_variables_module
from src.parse_text import ParseTextJS, ParseTextTwee
from src.tools.process_variables import VariablesProcess


class ParseTextTweeTests(unittest.TestCase):
    def test_multiline_set_and_run_expressions_mark_every_source_line(self):
        lines = [
            ":: Example\n",
            "<<set $message to {\n",
            '    text: "A new line of dialogue",\n',
            "\n",
            "}>>\n",
            "<<run $items.push(\n",
            '    "A translated item"\n',
            "\n",
            ")>>\n",
            "This is ordinary passage text.\n",
        ]
        filepath = Path("game/loc-example/example.twee")
        expressions = [
            '<<set $message to {\n    text: "A new line of dialogue",\n\n}>>',
            '<<run $items.push(\n    "A translated item"\n\n)>>',
        ]
        parser = ParseTextTwee(lines, filepath)

        with patch.object(
            parse_text_module.VariablesProcess,
            "fetch_all_file_paths",
            return_value={filepath},
        ), patch.object(
            parse_text_module.VariablesProcess,
            "fetch_all_set_content",
            return_value=[{"path": str(filepath), "lines": expressions}],
        ):
            flags = parser.pre_parse_set_run()

        self.assertEqual(
            flags,
            [False, True, True, False, True, True, True, False, True, False],
        )


class VariablesProcessTests(unittest.TestCase):
    def test_stale_set_run_cache_is_rebuilt_when_source_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "sample.twee"
            source.write_text('<<set $value to "first">>\n', encoding="utf-8")
            cache_dir = root / "setto"
            cache_dir.mkdir()
            cache_file = cache_dir / "_set_contents.json"
            cache_file.write_text(
                json.dumps([{"path": str(source), "lines": ['<<set $value to "stale">>']}]),
                encoding="utf-8",
            )

            with patch.object(process_variables_module, "SELF_ROOT", root), patch.object(
                process_variables_module, "set_CONTENTS", None
            ), patch.object(
                process_variables_module, "set_CONTENTS_FINGERPRINT", None
            ):
                first_processor = VariablesProcess()
                first_processor._all_file_paths = {source}
                first = first_processor.fetch_all_set_content()
                self.assertTrue(cache_file.exists())
                self.assertEqual(first[0]["lines"], ['<<set $value to "first">>'])
                self.assertEqual(len(first), 1)

                source.write_text('<<set $value to "second">>\n', encoding="utf-8")
                process_variables_module.set_CONTENTS = None
                process_variables_module.set_CONTENTS_FINGERPRINT = None
                second_processor = VariablesProcess()
                second_processor._all_file_paths = {source}
                second = second_processor.fetch_all_set_content()

            self.assertEqual(second[0]["lines"], ['<<set $value to "second">>'])
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(cache["data"], second)


class ParseTextJSTests(unittest.TestCase):
    def parse_lines(self, source: str, filepath: str) -> tuple[list[str], list[bool]]:
        lines = source.splitlines(keepends=True)
        return lines, ParseTextJS(lines, Path(filepath)).parse()

    def test_foodstuff_extracts_chinese_fields_and_multiline_ingredients(self):
        source = '''\
setup.foodstuff = {
    bread: {
        name: "bread",
        singular: "面包",
        plural: "面包",
        category: "dish", category_cn: "菜肴",
        ingredients: [
            "flour",
            "salt",
        ],
        ingredients_cn: [
            "面粉",
            "盐",
        ],
        icon: "bread.png",
    },
};
'''
        lines, selected = self.parse_lines(source, "game/04-Variables/foodstuff.js")

        expected_selected = {
            '        name: "bread",',
            '        singular: "面包",',
            '        plural: "面包",',
            '        category: "dish", category_cn: "菜肴",',
            '        ingredients: [',
            '            "flour",',
            '            "salt",',
            '        ingredients_cn: [',
            '            "面粉",',
            '            "盐",',
        }
        selected_lines = {
            line.rstrip("\n") for line, include in zip(lines, selected) if include
        }
        self.assertTrue(expected_selected.issubset(selected_lines))
        self.assertNotIn('        icon: "bread.png",', selected_lines)

    def test_foodstuff_extracts_single_line_ingredient_arrays(self):
        source = '''\
setup.foodstuff = {
    bread: {
        ingredients: ["flour", "salt"],
        ingredients_cn: ["面粉", "盐"],
    },
};
'''
        lines, selected = self.parse_lines(source, "game/04-Variables/foodstuff.js")
        selected_lines = {
            line.rstrip("\n") for line, include in zip(lines, selected) if include
        }

        self.assertIn('        ingredients: ["flour", "salt"],', selected_lines)
        self.assertIn('        ingredients_cn: ["面粉", "盐"],', selected_lines)

    def test_story_functions_extracts_new_name_and_risk_labels(self):
        source = '''\
function pregnancyNameCorrection(name) {
    case "pc":
        name = "你自己";
        break;
    default:
        name = (_name && _role) ? setup.NPC_CN_NAME(_name) : name;
}
const riskyBound = labels[labels.findIndex(l => l.text === "risky") - 1].upTo;
return {
    today: labels[playerPregnancyRisk()].text,
};
return pregnancy;
'''
        lines, selected = self.parse_lines(
            source, "game/03-JavaScript/04-Pregnancy/story-functions.js"
        )

        selected_lines = {
            line.rstrip("\n") for line, include in zip(lines, selected) if include
        }
        self.assertIn('        name = "你自己";', selected_lines)
        self.assertIn(
            "        name = (_name && _role) ? setup.NPC_CN_NAME(_name) : name;",
            selected_lines,
        )
        self.assertIn(
            'const riskyBound = labels[labels.findIndex(l => l.text === "risky") - 1].upTo;',
            selected_lines,
        )
        self.assertIn('    today: labels[playerPregnancyRisk()].text,', selected_lines)
        self.assertNotIn("return pregnancy;", selected_lines)


if __name__ == "__main__":
    unittest.main()
