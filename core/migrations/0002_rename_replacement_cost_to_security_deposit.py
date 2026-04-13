"""Rename GameTitle.replacement_cost to security_deposit."""

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="gametitle",
            old_name="replacement_cost",
            new_name="security_deposit",
        ),
        migrations.AlterField(
            model_name="gametitle",
            name="security_deposit",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Security deposit in INR required to borrow this game.",
                max_digits=10,
                validators=[django.core.validators.MinValueValidator(0)],
            ),
        ),
    ]
