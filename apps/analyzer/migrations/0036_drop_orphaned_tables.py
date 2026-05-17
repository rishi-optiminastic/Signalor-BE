"""
Drop tables that exist in the database but have no corresponding model in models.py.
Several of these (brandkit, chatmessage, contentsuggestion, backlinkorder,
domainanalyticssnapshot, sitebacklinkopportunity) hold FK constraints that point at
analyzer_analysisrun, which caused IntegrityError / 500 on account deletion.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("analyzer", "0035_merge_20260424_0957"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                DROP TABLE IF EXISTS analyzer_brandkit CASCADE;
                DROP TABLE IF EXISTS analyzer_chatmessage CASCADE;
                DROP TABLE IF EXISTS analyzer_contentsuggestion CASCADE;
                DROP TABLE IF EXISTS analyzer_domainanalyticssnapshot CASCADE;
                DROP TABLE IF EXISTS analyzer_sitebacklinkopportunity CASCADE;
                DROP TABLE IF EXISTS analyzer_backlinkorder CASCADE;
                DROP TABLE IF EXISTS analyzer_backlinkopportunity CASCADE;
                DROP TABLE IF EXISTS analyzer_backlinkproduct CASCADE;
                DROP TABLE IF EXISTS analyzer_backlinkprovider CASCADE;
                DROP TABLE IF EXISTS analyzer_backlinksnapshot CASCADE;
                DROP TABLE IF EXISTS analyzer_promptschemaartifact CASCADE;
                DROP TABLE IF EXISTS analyzer_promptwikipediadraft CASCADE;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
