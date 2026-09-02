# Локальный каталог РЛС

Medicine Cabinet разделяет **справочник лекарств** и **домашние остатки**:

```text
/config/medicine_cabinet/medicine_catalog.sqlite   # справочник, только чтение
/config/.storage/medicine_cabinet.data             # упаковки и настройки пользователя
```

Поэтому `medicine_catalog.sqlite` можно пересоздавать и заменять, не теряя домашние упаковки, историю и остатки.

## Создание каталога

В репозитории есть:

```text
tools/convert_rls.py
tools/build_catalog.bat
```

Для РЛС 2026 скрипт по умолчанию ориентирован на:

```text
C:\ProgramData\ENC2026\DB\rls.sqlite
C:\ProgramData\ENC2026\DB\rls_config.db
```

На Windows можно запустить:

```text
tools\build_catalog.bat
```

Или вручную:

```powershell
py -3 tools\convert_rls.py `
  --rls "C:\ProgramData\ENC2026\DB\rls.sqlite" `
  --config "C:\ProgramData\ENC2026\DB\rls_config.db" `
  --out "medicine_catalog.sqlite"
```

После создания скопируйте файл в Home Assistant:

```text
/config/medicine_cabinet/medicine_catalog.sqlite
```

Папку `/config/medicine_cabinet/` при необходимости создайте вручную.

## Что переносит конвертер

Конвертер переносит только данные, используемые интеграцией: штрихкоды, название, форму, дозировку, действующее вещество, производителя, АТХ, условия хранения, текстовые разделы описания/инструкции и компактный индекс лекарственных позиций для аналогов.

Поля изображений/BLOB не переносятся.

## Авторские права

Репозиторий не содержит и не распространяет базу РЛС или готовый каталог, созданный из неё. Скрипт предназначен для локальной обработки базы, установленной у пользователя. Условия использования исходных данных определяются правообладателем РЛС.
