"""24-hour date and time formats for every rendered datetime.

`LANGUAGE_CODE = "en-us"` is what makes `floatformat` emit a decimal point, but
it also brings `django.conf.locale.en.formats`, whose `DATETIME_FORMAT` is
`N j, Y, P` — `Aug. 14, 2026, 3:07 p.m.`. This app writes timestamps as
`Y-m-d H:i:s` everywhere an explicit filter was used, so the locale default was
the odd one out: whether a page showed a 24-hour clock came down to whether
whoever wrote the template remembered `|date:`. Overriding the format module
makes 24-hour the default and leaves a bare `{{ value }}` correct.

Only the display formats are named here. `DATE_INPUT_FORMATS`, the separators
and the number formats fall through to the `en` locale unchanged, which is what
keeps decimal points decimal points.
"""

DATETIME_FORMAT = "Y-m-d H:i:s"
DATE_FORMAT = "Y-m-d"
TIME_FORMAT = "H:i:s"
SHORT_DATETIME_FORMAT = "Y-m-d H:i"
SHORT_DATE_FORMAT = "Y-m-d"
