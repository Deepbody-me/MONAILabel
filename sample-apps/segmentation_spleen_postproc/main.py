import json
import logging
import os

from lib import (
    MyStrategy,
    MyTrain,
    SegmentationWithWriteLogits,
    SpleenBIFSegCRF,
    SpleenBIFSegGraphCut,
    SpleenBIFSegSimpleCRF,
    SpleenInteractiveGraphCut,
)
from monai.networks.layers import Norm
from monai.networks.nets import UNet

from monailabel.interfaces import MONAILabelApp
from monailabel.interfaces.tasks import InferType
from monailabel.utils.activelearning import Random

logger = logging.getLogger(__name__)


class MyApp(MONAILabelApp):
    def __init__(self, app_dir, studies):
        self.model_dir = os.path.join(app_dir, "model")
        self.network = UNet(
            dimensions=3,
            in_channels=1,
            out_channels=2,
            channels=(16, 32, 64, 128, 256),
            strides=(2, 2, 2, 2),
            num_res_units=2,
            norm=Norm.BATCH,
        )

        self.pretrained_model = os.path.join(self.model_dir, "segmentation_spleen.pt")
        self.final_model = os.path.join(self.model_dir, "final.pt")
        self.train_stats_path = os.path.join(self.model_dir, "train_stats.json")

        path = [self.pretrained_model, self.final_model]
        infers = {
            "Spleen_Segmentation": SegmentationWithWriteLogits(path, self.network),
            "BIFSeg+CRF": SpleenBIFSegCRF(),
            "BIFSeg+SimpleCRF": SpleenBIFSegSimpleCRF(),
            "BIFSeg+GraphCut": SpleenBIFSegGraphCut(),
            "Int.+BIFSeg+GraphCut": SpleenInteractiveGraphCut(),
        }

        strategies = {
            "random": Random(),
            "first": MyStrategy(),
        }

        resources = [
            (self.pretrained_model, "https://www.dropbox.com/s/xc9wtssba63u7md/segmentation_spleen.pt?dl=1"),
        ]

        super().__init__(
            app_dir=app_dir,
            studies=studies,
            infers=infers,
            strategies=strategies,
            resources=resources,
        )

        # Simple way to Add deepgrow 2D+3D models for infer tasks
        self.add_deepgrow_infer_tasks()

    def infer(self, request, datastore=None):
        image = request.get("image")

        # check if inferer is Post Processor
        if self.infers[request.get("model")].type == InferType.POSTPROCS:
            saved_labels = self.datastore().get_labels_by_image_id(image)
            for label, tag in saved_labels.items():
                if tag == "logits":
                    request["logits"] = self.datastore().get_label_uri(label)
            logger.info(f"Updated request: {request}")

        result = super().infer(request)
        result_params = result.get("params")

        logits = result_params.get("logits")
        if logits:
            self.datastore().save_label(image, logits, "logits")
            os.unlink(logits)

        result_params.pop("logits", None)
        logger.info(f"Final Result: {result}")
        return result

    def train(self, request):
        logger.info(f"Training request: {request}")

        output_dir = os.path.join(self.model_dir, request.get("name", "model_01"))

        # App Owner can decide which checkpoint to load (from existing output folder or from base checkpoint)
        load_path = os.path.join(output_dir, "model.pt")
        load_path = load_path if os.path.exists(load_path) else self.pretrained_model

        # Datalist for train/validation
        train_d, val_d = self.partition_datalist(self.datastore().datalist(), request.get("val_split", 0.2))

        task = MyTrain(
            output_dir=output_dir,
            train_datalist=train_d,
            val_datalist=val_d,
            network=self.network,
            load_path=load_path,
            publish_path=self.final_model,
            stats_path=self.train_stats_path,
            device=request.get("device", "cuda"),
            lr=request.get("lr", 0.0001),
            val_split=request.get("val_split", 0.2),
            max_epochs=request.get("epochs", 1),
            amp=request.get("amp", True),
        )
        return task()

    def train_stats(self):
        if os.path.exists(self.train_stats_path):
            with open(self.train_stats_path, "r") as fc:
                return json.load(fc)
        return super().train_stats()