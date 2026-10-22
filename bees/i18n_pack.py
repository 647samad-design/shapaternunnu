"""Built-in translations for the shop's own text (not product content).

Each entry: English -> (Urdu, Roman Urdu, Arabic, Spanish, French).
{0}, {1}... stand for numbers, prices, dates and codes; {store} is the
store name. The owner can override or add any text in Admin > Translations.
"""
from .i18n_countries import COUNTRY_ENTRIES
from .i18n_pack_data import ENTRIES
from .i18n_pack_data2 import ENTRIES2

ORDER = ("ur", "roman", "ar", "es", "fr")

MONTHS = {
    "Jan": ("جنوری", "Jan", "يناير", "ene", "janv."), "Feb": ("فروری", "Feb", "فبراير", "feb", "févr."),
    "Mar": ("مارچ", "Mar", "مارس", "mar", "mars"), "Apr": ("اپریل", "Apr", "أبريل", "abr", "avr."),
    "May": ("مئی", "May", "مايو", "may", "mai"), "Jun": ("جون", "Jun", "يونيو", "jun", "juin"),
    "Jul": ("جولائی", "Jul", "يوليو", "jul", "juil."), "Aug": ("اگست", "Aug", "أغسطس", "ago", "août"),
    "Sep": ("ستمبر", "Sep", "سبتمبر", "sept", "sept."), "Oct": ("اکتوبر", "Oct", "أكتوبر", "oct", "oct."),
    "Nov": ("نومبر", "Nov", "نوفمبر", "nov", "nov."), "Dec": ("دسمبر", "Dec", "ديسمبر", "dic", "déc."),
    "January": ("جنوری", "January", "يناير", "enero", "janvier"), "February": ("فروری", "February", "فبراير", "febrero", "février"),
    "March": ("مارچ", "March", "مارس", "marzo", "mars"), "April": ("اپریل", "April", "أبريل", "abril", "avril"),
    "June": ("جون", "June", "يونيو", "junio", "juin"), "July": ("جولائی", "July", "يوليو", "julio", "juillet"),
    "August": ("اگست", "August", "أغسطس", "agosto", "août"), "September": ("ستمبر", "September", "سبتمبر", "septiembre", "septembre"),
    "October": ("اکتوبر", "October", "أكتوبر", "octubre", "octobre"), "November": ("نومبر", "November", "نوفمبر", "noviembre", "novembre"),
    "December": ("دسمبر", "December", "ديسمبر", "diciembre", "décembre"),
    "Mon": ("پیر", "Peer", "الإثنين", "lun", "lun."), "Tue": ("منگل", "Mangal", "الثلاثاء", "mar", "mar."),
    "Wed": ("بدھ", "Budh", "الأربعاء", "mié", "mer."), "Thu": ("جمعرات", "Jumeraat", "الخميس", "jue", "jeu."),
    "Fri": ("جمعہ", "Juma", "الجمعة", "vie", "ven."), "Sat": ("ہفتہ", "Hafta", "السبت", "sáb", "sam."),
    "Sun": ("اتوار", "Itwaar", "الأحد", "dom", "dim."),
    "Monday": ("پیر", "Peer", "الإثنين", "lunes", "lundi"), "Tuesday": ("منگل", "Mangal", "الثلاثاء", "martes", "mardi"),
    "Wednesday": ("بدھ", "Budh", "الأربعاء", "miércoles", "mercredi"), "Thursday": ("جمعرات", "Jumeraat", "الخميس", "jueves", "jeudi"),
    "Friday": ("جمعہ", "Juma", "الجمعة", "viernes", "vendredi"), "Saturday": ("ہفتہ", "Hafta", "السبت", "sábado", "samedi"),
    "Sunday": ("اتوار", "Itwaar", "الأحد", "domingo", "dimanche"),
}

PACK = {}
for _source, _values in list(COUNTRY_ENTRIES.items()) + list(ENTRIES2.items()) + list(ENTRIES.items()) + list(MONTHS.items()):
    PACK[_source] = dict(zip(ORDER, _values))
