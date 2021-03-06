import boto3
import importlib
import json
import pyspark.sql
import sys
from awsglue.utils import getResolvedOptions
from es_aws_functions import aws_functions
from es_aws_functions import general_functions


bpm_queue_url = None
component = "spp-results-emr-glue-runner"
environment = "unconfigured"
logger = None
num_methods = 0
pipeline = "unconfigured"
run_id = None


def send_status(status, module_name, current_step_num=None):
    aws_functions.send_bpm_status(
        bpm_queue_url,
        module_name,
        status,
        run_id,
        survey=pipeline,
        current_step_num=current_step_num,
        total_steps=num_methods,
    )


try:
    args = getResolvedOptions(sys.argv, [
        "bpm_queue_url",
        "environment",
        "pipeline",
        "run_id",
        "snapshot_location"
    ])
    bpm_queue_url = args["bpm_queue_url"]
    environment = args["environment"]
    run_id = args["run_id"]
    pipeline = args["pipeline"]
    snapshot_location = args["snapshot_location"]

    s3 = boto3.resource("s3", region_name="eu-west-2")
    config = json.load(
        s3.Object(
            f"spp-res-{environment}-config",
            f"{pipeline}.json"
        ).get()["Body"]
    )

    methods = config["methods"]
    num_methods = len(methods)

    logger = general_functions.get_logger(
        pipeline,
        component,
        environment,
        run_id
    )

    logger.info(
        "Running pipeline %s with snapshot %s",
        pipeline,
        snapshot_location
    )
    # Set up extra params for ingest provided at runtime
    extra_ingest_params = {
        "run_id": run_id,
        "snapshot_location": snapshot_location
    }
    ingest_params = methods[0].get("params", {})
    ingest_params.update(extra_ingest_params)
    methods[0]["params"] = ingest_params

    spark = (
        pyspark.sql.SparkSession.builder.enableHiveSupport()
        .appName(pipeline)
        .getOrCreate()
    )
    spark.sql("SET spark.sql.sources.partitionOverwriteMode=dynamic")
    spark.sql("SET hive.exec.dynamic.partition.mode=nonstrict")

    send_status("IN PROGRESS", pipeline)
    # The first method is expected to get its data location at runtime whereas
    # subsequent methods must get their input from the output table of the
    # previous method
    data_location = None
    for method_num, method in enumerate(methods):
        # method_num is 0-indexed but we probably want step numbers
        # to be 1-indexed
        step_num = method_num + 1
        send_status(
            "IN PROGRESS",
            method["name"],
            current_step_num=step_num
        )
        logger.info("Starting method %s.%s", method["module"], method["name"])
        method_params = method.get("params", {})
        if method.get("provide_session"):
            method_params["spark"] = spark

        if data_location is not None:
            # Contains location of previous method's output
            method_params["df"] = spark.table(data_location).filter(
                pyspark.sql.functions.col("run_id") == run_id)

        module = importlib.import_module(method["module"])

        output = getattr(module, method["name"])(**method_params)

        if output.count() == 0:
            raise RuntimeError(
                f"{method['module']}.{method['name']} returned 0 rows")

        data_location = method["data_target"]
        # We need to select the relevant columns from the output
        # to support differing column orders and so that we get
        # only the columns we want in our output tables
        (output.select(spark.table(data_location).columns)
            .write.insertInto(data_location, overwrite=True))

        send_status("DONE", method["name"], current_step_num=step_num)
        logger.info("Finished method %s.%s", method["module"], method["name"])

    send_status("DONE", pipeline)

except Exception:
    if logger is None:
        logger = general_functions.get_logger(
            pipeline,
            component,
            environment,
            None
        )

    logger.exception("Exception occurred in glue job")
    send_status("ERROR", pipeline)
    sys.exit(1)
