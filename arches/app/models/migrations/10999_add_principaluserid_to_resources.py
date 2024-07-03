# Generated by Django 2.2.6 on 2019-12-13 11:56

from django.conf import settings
from django.db import migrations, models
from django.db.models import deletion


class Migration(migrations.Migration):

    dependencies = [
        ("models", "10798_jsonld_importer"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourceinstance",
            name="principaluser",
            field=models.ForeignKey(
                on_delete=deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
                blank=True,
                null=True,
            ),
        ),
    ]
