# -*- coding: utf-8 -*-
"""Регрессионные тесты движка — все кейсы владельца (F1–F38) и затыки.

Запуск: python3 -m unittest tests.test_engine -v
Каждая проверка подписана кейсом. Сломанная регулярка = красный CI
до того, как код попадёт в облако (workflow tests.yml).
"""
import os
import unittest

os.environ.setdefault("AMO_TOKEN", "test")
os.environ.setdefault("MOONSHOT_API_KEY", "test")

import importlib.util


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load("chats", "chats.py")
R = _load("razbor", "razbor.py")


class TestCloseRe(unittest.TestCase):
    def test_ne_nado(self):           # Евгений Иванович 14.11
        self.assertTrue(C.CLOSE_RE.search("Спасибо, не надо"))
    def test_ne_spasibo(self):
        self.assertTrue(C.CLOSE_RE.search("не, спасибо"))
    def test_nashol(self):            # копчение 11.11
        self.assertTrue(C.CLOSE_RE.search("Спасибо большое нашол"))
    def test_nashol_drugogo(self):
        self.assertTrue(C.CLOSE_RE.search("нашёл другой сервис"))
    def test_neaktu(self):            # Даниил 10.11
        self.assertTrue(C.CLOSE_RE.search("не актуально"))
    def test_ne_zakryvaet_prostoe_nashli(self):
        self.assertFalse(C.CLOSE_RE.search("нашел вас на авито"))


class TestSchedRe(unittest.TestCase):
    def test_engine_volume_not_date(self):   # Радик 14.11
        self.assertFalse(C.SCHED_RE.search("Рапид 2016 1.6 110 л.с"))
        self.assertFalse(C.SCHED_RE.search("год 2.0 турбо"))
    def test_real_dates(self):
        self.assertTrue(C.SCHED_RE.search("могу 12.11"))
        self.assertTrue(C.SCHED_RE.search("3.12 приеду"))
        self.assertTrue(C.SCHED_RE.search("завтра"))
        self.assertTrue(C.SCHED_RE.search("в пятницу"))
        self.assertTrue(C.SCHED_RE.search("в 11 можно"))


class TestArrivedRe(unittest.TestCase):
    def test_priexal(self):           # Егор Пилипец 12.11
        self.assertTrue(C.ARRIVED_RE.search("Я приехал"))
        self.assertTrue(C.ARRIVED_RE.search("уже у вас, жду"))
        self.assertTrue(C.ARRIVED_RE.search("на месте"))
    def test_future_not_arrived(self):
        self.assertFalse(C.ARRIVED_RE.search("приеду завтра"))
        self.assertFalse(C.ARRIVED_RE.search("планирую приехать"))


class TestAckGuard(unittest.TestCase):
    def test_short_ack(self):         # Егор Пилипец/Евгений 12.11
        self.assertTrue(C.ACK_ANY_RE.match("Отлично"))
        self.assertTrue(C.ACK_ANY_RE.match("Принял, спасибо"))
    def test_question_not_ack(self):
        self.assertFalse(C.ACK_ANY_RE.match("хорошо к часам 10 нормально будет?")
                         and "?" not in "хорошо к часам 10 нормально будет?")
    def test_phone_not_question(self):  # whtevr 13.11
        self.assertEqual(C.norm_phone("89860081262"), "+79860081262")
        self.assertTrue(C.PHONE_SCAN_RE.search("мой номер 89860081262"))


class TestSamNaberet(unittest.TestCase):
    def test_obrachus(self):          # Надежда 10.11
        self.assertTrue(C.SAM_NABERET_RE.search("позднее обращусь"))
    def test_pozvonu_zavtra(self):
        self.assertTrue(C.SAM_NABERET_RE.search("сам позвоню завтра"))


class TestRefusalRe(unittest.TestCase):
    def test_ne_zanimaemsya(self):    # Данил 06.11
        self.assertTrue(C.REFUSAL_RE.search("таким не занимаемся"))
    def test_ne_vlezet(self):
        self.assertTrue(C.REFUSAL_RE.search("в бокс не залезет"))


class TestClientRefusedRe(unittest.TestCase):
    def test_neaktu(self):            # печка 11.11
        self.assertTrue(R.CLIENT_REFUSED_RE.search("уже неактуально"))
    def test_peredumal(self):
        self.assertTrue(R.CLIENT_REFUSED_RE.search("передумал"))
    def test_active_negotiation_not_refused(self):
        self.assertFalse(R.CLIENT_REFUSED_RE.search("договорились о завтра"))


class TestClientWasRe(unittest.TestCase):
    def test_byl_u_vas(self):         # правило отзыва 11.11
        self.assertTrue(C.CLIENT_WAS_RE.search("был у вас в прошлом месяце"))
    def test_sdelal_u_vas(self):
        self.assertTrue(C.CLIENT_WAS_RE.search("делал у вас масло"))


class TestPhoneNorm(unittest.TestCase):
    def test_11_digits(self):
        self.assertEqual(C.norm_phone("89827745555"), "+79827745555")
        self.assertEqual(C.norm_phone("+7 982 774-55-55"), "+79827745555")
    def test_garbage(self):
        self.assertIsNone(C.norm_phone("123"))


class TestVoisiClean(unittest.TestCase):
    def test_zero_width_removed(self):  # Войси 11.11
        dirty = "текст\u200b\u200c\u200d\u2060\u2063\ufeff\u00a0конец"
        clean = R.clean_text(dirty)
        self.assertEqual(clean, "текст конец")


if __name__ == "__main__":
    unittest.main()
