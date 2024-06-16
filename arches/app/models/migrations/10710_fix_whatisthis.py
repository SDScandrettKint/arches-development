# Generated by Django 4.2.13 on 2024-05-13 09:06

import textwrap

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("models", "10709_refresh_geos_by_transaction"),
    ]

    forward_sql = textwrap.dedent(
        # Single change is uuid() in SELECT
        """
        DROP VIEW IF EXISTS vw_annotations;

        CREATE OR REPLACE VIEW vw_annotations AS
        SELECT uuid(json_array_elements(t.tiledata::json->n.nodeid::text->'features')->>'id') as feature_id,
            t.tileid,
            t.tiledata,
            t.resourceinstanceid,
            t.nodegroupid,
            n.nodeid,
            json_array_elements(t.tiledata::json->n.nodeid::text->'features')::jsonb as feature,
            json_array_elements(t.tiledata::json->n.nodeid::text->'features')->'properties'->>'canvas' as canvas
        FROM tiles t
            LEFT JOIN nodes n ON t.nodegroupid = n.nodegroupid
        WHERE (
                (
                    SELECT count(*) AS count
                    FROM jsonb_object_keys(t.tiledata) jsonb_object_keys(jsonb_object_keys)
                    WHERE (
                            jsonb_object_keys.jsonb_object_keys IN (
                                SELECT n_1.nodeid::text AS nodeid
                                FROM nodes n_1
                                WHERE n_1.datatype = 'annotation'::text
                            )
                        )
                )
            ) > 0
        AND n.datatype = 'annotation'::text;
        """
    )

    reverse_sql = textwrap.dedent(
        """
        DROP VIEW IF EXISTS vw_annotations;

        CREATE OR REPLACE VIEW vw_annotations AS
        SELECT json_array_elements(t.tiledata::json->n.nodeid::text->'features')->>'id' as feature_id,
            t.tileid,
            t.tiledata,
            t.resourceinstanceid,
            t.nodegroupid,
            n.nodeid,
            json_array_elements(t.tiledata::json->n.nodeid::text->'features')::jsonb as feature,
            json_array_elements(t.tiledata::json->n.nodeid::text->'features')->'properties'->>'canvas' as canvas
        FROM tiles t
            LEFT JOIN nodes n ON t.nodegroupid = n.nodegroupid
        WHERE (
                (
                    SELECT count(*) AS count
                    FROM jsonb_object_keys(t.tiledata) jsonb_object_keys(jsonb_object_keys)
                    WHERE (
                            jsonb_object_keys.jsonb_object_keys IN (
                                SELECT n_1.nodeid::text AS nodeid
                                FROM nodes n_1
                                WHERE n_1.datatype = 'annotation'::text
                            )
                        )
                )
            ) > 0
        AND n.datatype = 'annotation'::text;
        """
    )

    operations = [migrations.RunSQL(forward_sql, reverse_sql)]
