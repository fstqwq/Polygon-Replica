# `app/service/importing`

Owns Native, Polygon, and ICPC archive admission and conversion into canonical
workspace source. Importers validate archive members and limits, interpret the
supported external layout, and replace the target workspace through the current
workspace service.

External compatibility input is normalized at this boundary; services do not
retain a second legacy workspace model.
