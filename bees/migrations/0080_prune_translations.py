from django.db import migrations


def prune(apps, schema_editor):
    # Built-in translations grew: clear rows that are now covered or are
    # product/store names, so Admin > Translations only lists real work.
    try:
        from bees.i18n import prune_missing
        prune_missing()
    except Exception:
        pass


class Migration(migrations.Migration):
    dependencies = [("bees", "0079_ops_b2b")]
    operations = [migrations.RunPython(prune, migrations.RunPython.noop)]
