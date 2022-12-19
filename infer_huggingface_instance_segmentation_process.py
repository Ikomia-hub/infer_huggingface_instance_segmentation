# Copyright (C) 2021 Ikomia SAS
# Contact: https://www.ikomia.com
#
# This file is part of the IkomiaStudio software.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from ikomia import core, dataprocess
import copy
# Your imports below
from transformers import AutoFeatureExtractor, AutoModelForInstanceSegmentation
from ikomia.utils import strtobool
import numpy as np
import torch
import numpy as np
import random
import cv2 


# --------------------
# - Class to handle the process parameters
# - Inherits PyCore.CWorkflowTaskParam from Ikomia API
# --------------------
class InferHuggingfaceInstanceSegmentationParam(core.CWorkflowTaskParam):

    def __init__(self):
        core.CWorkflowTaskParam.__init__(self)
        # Place default value initialization here
        self.cuda = torch.cuda.is_available()
        self.model_name = "facebook/maskformer-swin-base-coco"
        self.model_card =  "facebook/maskformer-swin-tiny-ade"
        self.conf_thres = 0.5
        self.background = False
        self.update = False

    def setParamMap(self, param_map):
        # Set parameters values from Ikomia application
        # Parameters values are stored as string and accessible like a python dict
        self.cuda = strtobool(param_map["cuda"])
        self.model_name = str(param_map["model_name"])
        self.model_card = str(param_map["model_card"])
        self.conf_thres = float(param_map["conf_thres"])
        self.background = strtobool(param_map["background_idx"])
        self.update = strtobool(param_map["update"])

    def getParamMap(self):
        # Send parameters values to Ikomia application
        # Create the specific dict structure (string container)
        param_map = core.ParamMap()
        param_map["cuda"] = str(self.cuda)
        param_map["model_name"]= str(self.model_name)
        param_map["model_card"] = str(self.model_card)
        param_map["background_idx"] = str(self.background)
        param_map["conf_thres"] = str(self.conf_thres)
        param_map["update"] = str(self.update)
        return param_map


# --------------------
# - Class which implements the process
# - Inherits PyCore.CWorkflowTask or derived from Ikomia API
# --------------------
class InferHuggingfaceInstanceSegmentation(dataprocess.C2dImageTask):

    def __init__(self, name, param):
        dataprocess.C2dImageTask.__init__(self, name)
        # Add input/output of the process here
        self.addOutput(dataprocess.CInstanceSegIO())

        # Create parameters class
        if param is None:
            self.setParam(InferHuggingfaceInstanceSegmentationParam())
        else:
            self.setParam(copy.deepcopy(param))

        # Detect if we have a GPU available
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.model = None
        self.model_id = None
        self.feature_extractor = None
        self.colors = None
        self.classes = None
        self.update = False

    def getProgressSteps(self):
        # Function returning the number of progress steps for this process
        # This is handled by the main progress bar of Ikomia application
        return 1

    def infer(self, image):
        param = self.getParam()

        # Image pre-pocessing (image transformation and conversion to PyTorch tensor)
        encoding = self.feature_extractor(image, return_tensors="pt")
        if param.cuda is True:
            #encoding = encoding.cuda()
            encoding = encoding.to(self.device)
        h, w, _ = np.shape(image)
        # Prediction
        with torch.no_grad():
            outputs = self.model(**encoding)
        results = self.feature_extractor.post_process_panoptic_segmentation(
                                                                        outputs,
                                                                        threshold = param.conf_thres,
                                                                        )[0]
        segments_info = results["segments_info"]

        # Get output :
        instance_output = self.getOutput(1)
        instance_output.init("PanopticSegmentation", 0, w, h)

        # dstImage
        dst_image = results["segmentation"].cpu().detach().numpy().astype(dtype=np.uint8)
        dst_image = cv2.resize(dst_image, (w,h), interpolation = cv2.INTER_NEAREST)

        # Generating binary masks for each object present in the groundtruth mask
        unique_colors = np.unique(dst_image).tolist()
        unique_colors = [x for x in unique_colors if x != 0]

        masks = np.zeros(dst_image.shape)
        mask_list = []
        for color in unique_colors:
            object_mask = np.where(dst_image == color, 1, 0)
            mask_list.append(object_mask)
            masks = np.dstack([object_mask, masks])

        # Get bounding boxes from masks
        boxes = []
        for i in range(masks.shape[-1]):
            m = masks[:, :, i]
            # Bounding box.
            horizontal_indicies = np.where(np.any(m, axis=0))[0]
            vertical_indicies = np.where(np.any(m, axis=1))[0]
            if horizontal_indicies.shape[0]:
                x1, x2 = horizontal_indicies[[0, -1]]
                y1, y2 = vertical_indicies[[0, -1]]
            boxes.append([x1, y1, x2, y2])
        boxes = boxes[:-1]
        boxes.reverse()
        mask_list.pop(0)

        # Add segmented instance to the output
        for i, b, ml in zip(segments_info, boxes, mask_list):
            x1 = (b[0] + b[2])/2
            instance_output.addInstance(
                                    i["id"],
                                    0,
                                    i["label_id"],
                                    self.classes[i["label_id"]],
                                    float(i["score"]),
                                    float(x1),
                                    float(b[1]),
                                    0,
                                    0,
                                    ml,
                                    self.colors[i["label_id"]]
                                    )

        self.forwardInputImage(0, 0)

    def run(self):
        # Core function of your process
        # Call beginTaskRun for initialization
        self.beginTaskRun()

        image_in = self.getInput(0)

        # Get image from input/output (numpy array):
        image = image_in.getImage()

        param = self.getParam()

        if param.update or self.model is None:
        # Feature extractor selection
            if param.model_card == "":
                param.model_card = None
            if param.model_name == "From: Costum model name":
                self.model_id = param.model_card
            else:
                self.model_id = param.model_name
                param.model_card = None
            self.feature_extractor = AutoFeatureExtractor.from_pretrained(self.model_id)

            # Loading model weight
            self.model = AutoModelForInstanceSegmentation.from_pretrained(self.model_id)
            self.device = torch.device("cuda") if param.cuda else torch.device("cpu")
            self.model.to(self.device)
            print("Will run on {}".format(self.device.type))

            # Get label name
            self.classes = list(self.model.config.id2label.values())

            # Color palette
            n = len(self.classes)
            random.seed(14)
            if param.background is True:
                self.colors = [[0,0,0]]
                for i in range(n-1):
                    self.colors.append(random.choices(range(256), k=3))
            else:
                self.colors = []
                for i in range(n):
                    self.colors.append(random.choices(range(256), k=3))
            self.setOutputColorMap(0, 1, self.colors)
            param.update = False

        # Inference
        self.infer(image)

        # Step progress bar:
        self.emitStepProgress()

        # Call endTaskRun to finalize process
        self.endTaskRun()


# --------------------
# - Factory class to build process object
# - Inherits PyDataProcess.CTaskFactory from Ikomia API
# --------------------
class InferHuggingfaceInstanceSegmentationFactory(dataprocess.CTaskFactory):

    def __init__(self):
        dataprocess.CTaskFactory.__init__(self)
        # Set process information as string here
        self.info.name = "infer_huggingface_instance_segmentation"
        self.info.shortDescription = "Instance segmentation using models from Hugging Face."
        self.info.description = "This plugin proposes inference for instance segmentation"\
                                "using transformers models from Hugging Face. It regroups"\
                                "models covered by the Hugging Face class:"\
                                "<AutoModelForInstanceSegmentation>. Models can be loaded either"\
                                "from your fine-tuned model (local) or from the Hugging Face Hub."
        # relative path -> as displayed in Ikomia application process tree
        self.info.path = "Plugins/Python/Segmentation"
        self.info.version = "1.0.0"
        self.info.iconPath = "icons/icon.png"
        self.info.authors = "Thomas Wolf, Lysandre Debut, Victor Sanh, Julien Chaumond,"\
                            "Clement Delangue, Anthony Moi, Pierric Cistac, Tim Rault,"\
                            "Rémi Louf, Morgan Funtowicz, Joe Davison, Sam Shleifer,"\
                            "Patrick von Platen, Clara Ma, Yacine Jernite, Julien Plu,"\
                            "Canwen Xu, Teven Le Scao, Sylvain Gugger, Mariama Drame,"\
                            "Quentin Lhoest, Alexander M. Rush"
        self.info.article = "Huggingface's Transformers: State-of-the-art Natural Language Processing"
        self.info.journal = "EMNLP"
        self.info.license = "Apache License Version 2.0"
        # URL of documentation
        self.info.documentationLink = "https://www.aclweb.org/anthology/2020.emnlp-demos.6"
        # Code source repository
        self.info.repository = "https://github.com/huggingface/transformers"
        # Keywords used for search
        self.info.keywords = "semantic, segmentation, inference, transformer,"\
                            "Hugging Face, Pytorch, Maskformer"

    def create(self, param=None):
        # Create process object
        return InferHuggingfaceInstanceSegmentation(self.info.name, param)