from django.db import migrations, models


ROLE_TAGS = (
    "Founder/CEO",
    "CFO/Finance",
    "COO/Operations",
    "CRO/Revenue",
    "Product Executive",
    "Technology/Engineering Executive",
    "CIO/Internal IT Executive",
    "Security/Trust Executive",
    "Product/Engineering N-1",
    "Security/Compliance N-1",
    "Compliance/Risk/Privacy Executive",
    "FedRAMP Owner/Operator",
    "GRC Manager/Lead",
    "GRC Engineer",
    "GRC Analyst/Practitioner",
    "Federal/Public Sector Executive",
    "Federal Sales/BD IC",
    "Federal Solutions Engineer/Architect",
    "Public Sector Partnerships/Alliances/CS",
    "Field CTO/CISO/Technical Evangelist",
)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0020_opportunity_active_account_admission"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="role_tag",
            field=models.CharField(
                blank=True,
                choices=[(value, value) for value in ROLE_TAGS],
                db_default="",
                db_index=True,
                default="",
                max_length=64,
            ),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.CheckConstraint(
                condition=models.Q(("role_tag", ""), ("role_tag__in", ROLE_TAGS), _connector="OR"),
                name="lead_role_tag_is_canonical",
            ),
        ),
    ]
