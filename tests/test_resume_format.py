import unittest

from resume_format import render, split_messages


class ResumeFormatTests(unittest.TestCase):
    def test_headings_bullets_and_wrapped_lines(self):
        text = render(["ОПЫТ РАБОТЫ", "Frontend-разработчик, 2021–2024", "• Делал на Vue 3", "и Nuxt",
                       "- Писал <тесты>", "Навыки:"])
        self.assertEqual(text, "<b>ОПЫТ РАБОТЫ</b>\nFrontend-разработчик, 2021–2024\n"
                               "• Делал на Vue 3 и Nuxt\n• Писал &lt;тесты&gt;\n\n<b>Навыки</b>")

    def test_split_keeps_lines_whole_and_under_limit(self):
        lines = [f"строка {i} " + "x" * 50 for i in range(200)]
        chunks = split_messages("\n".join(lines), limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("\n".join(chunks).split("\n"), lines)

    def test_short_resume_is_single_message(self):
        self.assertEqual(len(split_messages(render(["Обо мне", "Разработчик"]))), 1)

    def test_overlong_line_is_cut(self):
        chunks = split_messages("слово " * 1000, limit=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))


if __name__ == "__main__":
    unittest.main()
