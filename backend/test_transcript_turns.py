"""Offline regression tests: python -m unittest test_transcript_turns -v."""
import unittest
from types import SimpleNamespace
from app.core.stt import stt_to_segments
from app.core.transcribe import _segments_to_dicts
from app.core.meetings_mapper import to_transcript


class TranscriptTurnsTests(unittest.TestCase):
    def test_labelled_monolith(self):
        text = ('Заказчик: Нужна форма заявки. Разработчик: Куда отправлять данные? '
                'Заказчик: На почту менеджеру. Разработчик: Потребуется три дня.')
        rows = stt_to_segments({'segments': [{'start': 0, 'end': 79, 'text': text, 'speaker': 'Заказчик'}]})
        self.assertEqual([r.speaker for r in rows], ['Заказчик', 'Менеджер', 'Заказчик', 'Менеджер'])
        self.assertEqual([r.id for r in rows], list(range(4)))
        self.assertTrue(all(r.timing_estimated for r in rows))
        self.assertEqual(rows[-1].end, 79)
        self.assertTrue(all(a.end <= b.start for a, b in zip(rows, rows[1:])))
        self.assertTrue(all(r.timing_estimated for r in to_transcript(rows)))

    def test_real_segments_untouched(self):
        rows = stt_to_segments({'segments': [
            {'start': 2, 'end': 5, 'speaker': 'speaker_0', 'text': 'Нужна форма.'},
            {'start': 6, 'end': 9, 'speaker': 'speaker_1', 'text': 'Какие поля?'},
        ]})
        self.assertEqual([(r.start, r.end) for r in rows], [(2, 5), (6, 9)])
        self.assertFalse(any(r.timing_estimated for r in rows))

    def test_long_text_keeps_words_without_inventing_speakers(self):
        text = 'Это длинное предложение о проекте. ' * 40
        rows = stt_to_segments(text)
        self.assertGreater(len(rows), 1)
        self.assertEqual(' '.join(r.text for r in rows).split(), text.split())
        self.assertTrue(all(r.speaker is None for r in rows))

    def test_role_mention_does_not_create_a_turn(self):
        rows = stt_to_segments('Разработчик, проверьте форму. Заказчик согласовал поля.')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].speaker)

    def test_addresses_do_not_swap_roles(self):
        text = 'Менеджер, здравствуйте! Заказчик, уточните задачу.'
        rows = stt_to_segments({'segments': [{'text': text, 'start': 0, 'end': 10, 'speaker': 'speaker_0'}]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].text, text)
        self.assertEqual(rows[0].speaker, 'Говорящий 1')

    def test_explicit_single_label_overrides_wrong_provider_role(self):
        rows = stt_to_segments({'segments': [{'text': 'Менеджер: Какие поля?', 'start': 0, 'end': 2, 'speaker': 'Заказчик'}]})
        self.assertEqual(rows[0].speaker, 'Менеджер')
        self.assertEqual(rows[0].text, 'Какие поля?')
        self.assertFalse(rows[0].timing_estimated)

    def test_sdk_prefers_timed_sentences_over_aggregate(self):
        result = SimpleNamespace(segments=[SimpleNamespace(text='Весь разговор')], sentences=[
            SimpleNamespace(text='Нужна форма.', time='00:00:02', speaker='speaker_0'),
            SimpleNamespace(text='Какие поля?', time='00:00:05', speaker='speaker_1'),
        ])
        rows = stt_to_segments({'segments': _segments_to_dicts(result)})
        self.assertEqual([r.start for r in rows], [2, 5])
        self.assertEqual(len(rows), 2)

    def test_nexara_receives_role_descriptions(self):
        from unittest.mock import patch
        from pathlib import Path
        from app.config import Settings
        from app.core.transcribe import _nexara_sync
        with patch('nexara.Nexara') as client:
            client.return_value.transcriptions.create.return_value = {'segments': [{'text': 'Нужна форма.', 'start': 0, 'end': 2, 'speaker': 'Заказчик'}]}
            rows = _nexara_sync(Path('test.mp3'), Settings(nexara_roles='Заказчик,Менеджер', nexara_api_key='test'), True)
            args = client.return_value.transcriptions.create.call_args.kwargs
            self.assertEqual(set(args['roles']), {'Заказчик', 'Менеджер'})
            self.assertIsInstance(args['roles'], dict)
            self.assertEqual(rows[0]['speaker'], 'Заказчик')

    def test_sentence_fallback_does_not_drop_speaker(self):
        result = {'segments': [{'text': 'Нужна форма. С отправкой на почту.', 'start': 0, 'end': 5, 'speaker': 'Заказчик'}],
                  'sentences': [{'text': 'Нужна форма.', 'start': 0, 'end': 2}, {'text': 'С отправкой на почту.', 'start': 2, 'end': 5}]}
        rows = stt_to_segments({'segments': _segments_to_dicts(result)})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].speaker, 'Заказчик')
        self.assertIn('С отправкой на почту.', rows[0].text)

    def test_dict_response_preserves_speaker_zero(self):
        raw = _segments_to_dicts({'segments': [{'start': 0, 'end': 2, 'text': 'Здравствуйте.', 'speaker_id': 0}]})
        self.assertEqual(stt_to_segments({'segments': raw})[0].speaker, 'Говорящий 1')


if __name__ == '__main__':
    unittest.main()
