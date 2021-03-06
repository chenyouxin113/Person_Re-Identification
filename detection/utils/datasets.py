import os
import numpy as np
import torch
import cv2
import glob
import random
import math
from torch.utils.data import Dataset
from .commons import xyxy2xywh


class LoadCamera:  # for inference
    def __init__(self, file_path, img_size=416, resize_mode='square'):
        self.cam = cv2.VideoCapture(file_path)
        self.height = img_size
        self.resize_mode = resize_mode
        self.path = file_path

    def __iter__(self):
        self.frame = 0
        return self

    def __next__(self):
        self.frame += 1
        # Read image
        ret_val, img0 = self.cam.read()
        assert ret_val, 'Webcam Error'

        return None, pre_process(img0, self.height, self.resize_mode), img0

    def __len__(self):
        return 0


class LoadImages:  # for inference
    def __init__(self, path, img_size=416, resize_mode='square', od_model='yolo'):
        self.height = img_size
        img_formats = ['.jpg', '.jpeg', '.png', '.tif']
        vid_formats = ['.mov', '.avi', '.mp4']

        files = []
        if os.path.isdir(path):
            files = sorted(glob.glob('%s/*' % path))
        elif os.path.isfile(path):
            files = [path]

        images = [x for x in files if os.path.splitext(x)[-1].lower() in img_formats]
        videos = [x for x in files if os.path.splitext(x)[-1].lower() in vid_formats]
        images_folder = [x for x in files if os.path.isdir(x)]

        nI, nV, nIF = len(images), len(videos), len(images_folder)

        self.files = images + videos + images_folder
        self.nF = nI + nV + nIF  # number of files
        self.video_flag = [False] * nI + [True] * (nV + nIF)
        self.mode = 'I'
        self.resize_mode = resize_mode
        self.od_model = od_model
        if any(videos):
            self.new_video(videos[0])  # new video
            self.mode = 'V'
        else:
            if any(images_folder):
                self.new_video(images_folder[0], False)
                self.mode = 'IF'
            else:
                self.cap = None
        assert self.nF > 0, 'No images or videos found in ' + path

    def __iter__(self):
        self.count = 0
        return self

    def __next__(self):
        if self.count == self.nF:
            raise StopIteration

        path = self.files[self.count]

        if self.video_flag[self.count]:
            # Read video
            if self.mode == 'V':
                ret_val, img0 = self.cap.read()
            else:
                ret_val = len(self.cap) > self.frame
                if ret_val:
                    img0 = cv2.imread(self.cap[self.frame])

            if not ret_val:
                self.count += 1

                if self.mode == 'V':
                    self.cap.release()

                if self.count == self.nF:  # last video
                    raise StopIteration
                else:
                    path = self.files[self.count]
                    if self.mode == 'V':
                        self.new_video(path)
                        ret_val, img0 = self.cap.read()
                    else:
                        self.new_video(path, False)
                        img0 = cv2.imread(self.cap[self.frame])

            self.frame += 1
            self.path = path
            print('video %g/%g (%g/%g) %s: ' % (self.count + 1, self.nF, self.frame, self.nframes, path))
        else:
            # Read image
            self.count += 1
            img0 = cv2.imread(path)  # BGR
            assert img0 is not None, 'File Not Found ' + path
            print('image %g/%g %s: ' % (self.count, self.nF, path))

        if self.od_model == 'yolo':
            img = pre_process(img0, self.height, self.resize_mode)
        else:
            img = img0.copy()

        return path, img, img0

    def new_video(self, path, video=True):
        self.frame = 0
        if video:
            self.cap = cv2.VideoCapture(path)
            self.nframes = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        else:
            paths = sorted(glob.glob('%s/*.*' % path))
            self.cap = paths
            self.nframes = len(paths)

    def __len__(self):
        return self.nF  # number of files


class LoadImagesAndLabels(Dataset):  # for training/testing
    def __init__(self, path, img_size=416, augment=False):
        with open(path, 'r') as file:
            self.img_files = file.read().splitlines()
            self.img_files = list(filter(lambda x: len(x) > 0, self.img_files))
        assert len(self.img_files) > 0, 'No images found in %s' % path
        self.img_size = img_size
        self.augment = augment
        self.label_files = [
            x.replace('images', 'labels').replace('.bmp', '.txt').replace('.jpg', '.txt').replace('.png', '.txt')
            for x in self.img_files]

    def __len__(self):
        return len(self.img_files)

    def __getitem__(self, index):
        img_path = self.img_files[index]
        label_path = self.label_files[index]

        img = cv2.imread(img_path)  # BGR
        assert img is not None, 'File Not Found ' + img_path

        augment_hsv = True
        if self.augment and augment_hsv:
            # SV augmentation by 50%
            fraction = 0.50  # must be < 1.0
            img_hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            S = img_hsv[:, :, 1].astype(np.float32)
            V = img_hsv[:, :, 2].astype(np.float32)

            a = (random.random() * 2 - 1) * fraction + 1
            S *= a
            if a > 1:
                np.clip(S, None, 255, out=S)

            a = (random.random() * 2 - 1) * fraction + 1
            V *= a
            if a > 1:
                np.clip(V, None, 255, out=V)

            img_hsv[:, :, 1] = S  # .astype(np.uint8)
            img_hsv[:, :, 2] = V  # .astype(np.uint8)
            cv2.cvtColor(img_hsv, cv2.COLOR_HSV2BGR, dst=img)

        h, w, _ = img.shape
        img, ratio, padw, padh = letterbox(img, self.img_size)

        # Load labels
        labels = []
        if os.path.isfile(label_path):
            with open(label_path, 'r') as file:
                lines = file.read().splitlines()

            x = np.array([x.split() for x in lines], dtype=np.float32)
            if x.size > 0:
                # Normalized xywh to pixel xyxy format
                labels = x.copy()
                labels[:, 1] = ratio * w * (x[:, 1] - x[:, 3] / 2) + padw
                labels[:, 2] = ratio * h * (x[:, 2] - x[:, 4] / 2) + padh
                labels[:, 3] = ratio * w * (x[:, 1] + x[:, 3] / 2) + padw
                labels[:, 4] = ratio * h * (x[:, 2] + x[:, 4] / 2) + padh

        # Augment image and labels
        if self.augment:
            img, labels = random_affine(img, labels, degrees=(-5, 5), translate=(0.10, 0.10), scale=(0.90, 1.10))

        nL = len(labels)  # number of labels
        if nL:
            # convert xyxy to xywh
            labels[:, 1:5] = xyxy2xywh(labels[:, 1:5]) / self.img_size

        if self.augment:
            # random left-right flip
            lr_flip = True
            if lr_flip and random.random() > 0.5:
                img = np.fliplr(img)
                if nL:
                    labels[:, 1] = 1 - labels[:, 1]

            # random up-down flip
            ud_flip = False
            if ud_flip and random.random() > 0.5:
                img = np.flipud(img)
                if nL:
                    labels[:, 2] = 1 - labels[:, 2]

        labels_out = torch.zeros((nL, 6))
        if nL:
            labels_out[:, 1:] = torch.from_numpy(labels)

        # Normalize
        img = img[:, :, ::-1].transpose(2, 0, 1)  # BGR to RGB, to 3x416x416
        img = np.ascontiguousarray(img, dtype=np.float32)  # uint8 to float32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0

        return torch.from_numpy(img), labels_out, img_path, (h, w)

    @staticmethod
    def collate_fn(batch):
        img, label, path, hw = list(zip(*batch))  # transposed
        for i, l in enumerate(label):
            l[:, 0] = i  # add target image index for build_targets()
        return torch.stack(img, 0), torch.cat(label, 0), path, hw


def random_affine(img, targets=(), degrees=(-10, 10), translate=(.1, .1), scale=(.9, 1.1), shear=(-2, 2),
                  borderValue=(127.5, 127.5, 127.5)):
    # torchvision.transforms.RandomAffine(degrees=(-10, 10), translate=(.1, .1), scale=(.9, 1.1), shear=(-10, 10))
    # https://medium.com/uruvideo/dataset-augmentation-with-random-homographies-a8f4b44830d4

    if targets is None:
        targets = []
    border = 0  # width of added border (optional)
    height = max(img.shape[0], img.shape[1]) + border * 2

    # Rotation and Scale
    R = np.eye(3)
    a = random.random() * (degrees[1] - degrees[0]) + degrees[0]
    # a += random.choice([-180, -90, 0, 90])  # 90deg rotations added to small rotations
    s = random.random() * (scale[1] - scale[0]) + scale[0]
    R[:2] = cv2.getRotationMatrix2D(angle=a, center=(img.shape[1] / 2, img.shape[0] / 2), scale=s)

    # Translation
    T = np.eye(3)
    T[0, 2] = (random.random() * 2 - 1) * translate[0] * img.shape[0] + border  # x translation (pixels)
    T[1, 2] = (random.random() * 2 - 1) * translate[1] * img.shape[1] + border  # y translation (pixels)

    # Shear
    S = np.eye(3)
    S[0, 1] = math.tan((random.random() * (shear[1] - shear[0]) + shear[0]) * math.pi / 180)  # x shear (deg)
    S[1, 0] = math.tan((random.random() * (shear[1] - shear[0]) + shear[0]) * math.pi / 180)  # y shear (deg)

    M = S @ T @ R  # Combined rotation matrix. ORDER IS IMPORTANT HERE!!
    imw = cv2.warpPerspective(img, M, dsize=(height, height), flags=cv2.INTER_LINEAR,
                              borderValue=borderValue)  # BGR order borderValue

    # Return warped points also
    if len(targets) > 0:
        n = targets.shape[0]
        points = targets[:, 1:5].copy()
        area0 = (points[:, 2] - points[:, 0]) * (points[:, 3] - points[:, 1])

        # warp points
        xy = np.ones((n * 4, 3))
        xy[:, :2] = points[:, [0, 1, 2, 3, 0, 3, 2, 1]].reshape(n * 4, 2)  # x1y1, x2y2, x1y2, x2y1
        xy = (xy @ M.T)[:, :2].reshape(n, 8)

        # create new boxes
        x = xy[:, [0, 2, 4, 6]]
        y = xy[:, [1, 3, 5, 7]]
        xy = np.concatenate((x.min(1), y.min(1), x.max(1), y.max(1))).reshape(4, n).T

        # apply angle-based reduction of bounding boxes
        radians = a * math.pi / 180
        reduction = max(abs(math.sin(radians)), abs(math.cos(radians))) ** 0.5
        x = (xy[:, 2] + xy[:, 0]) / 2
        y = (xy[:, 3] + xy[:, 1]) / 2
        w = (xy[:, 2] - xy[:, 0]) * reduction
        h = (xy[:, 3] - xy[:, 1]) * reduction
        xy = np.concatenate((x - w / 2, y - h / 2, x + w / 2, y + h / 2)).reshape(4, n).T

        # reject warped points outside of image
        np.clip(xy, 0, height, out=xy)
        w = xy[:, 2] - xy[:, 0]
        h = xy[:, 3] - xy[:, 1]
        area = w * h
        ar = np.maximum(w / (h + 1e-16), h / (w + 1e-16))
        i = (w > 4) & (h > 4) & (area / (area0 + 1e-16) > 0.1) & (ar < 10)

        targets = targets[i]
        targets[:, 1:5] = xy[i]

    return imw, targets


def letterbox(img, new_shape=416, color=(127.5, 127.5, 127.5), mode='auto'):
    """Resize a rectangular image to a 32 pixel multiple rectangle"""
    shape = img.shape[:2]  # shape = [height, width]
    if isinstance(new_shape, int):
        ratio = float(new_shape) / max(shape)
    else:
        ratio = max(new_shape) / max(shape)

    new_unpad = (int(round(shape[1] * ratio)), int(round(shape[0] * ratio)))
    if mode == 'auto':  # minimum rectangle
        dw = np.mod(new_shape - new_unpad[0], 32) / 2  # width padding
        dh = np.mod(new_shape - new_unpad[1], 32) / 2  # height padding
    elif mode == 'square':  # square
        dw = (new_shape - new_unpad[0]) / 2  # width padding
        dh = (new_shape - new_unpad[1]) / 2  # height padding
    elif mode == 'rect':  # square
        dw = (new_shape[1] - new_unpad[0]) / 2  # width padding
        dh = (new_shape[0] - new_unpad[1]) / 2  # height padding

    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)  # resized, no border
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)  # padded square
    return img, ratio, dw, dh


def pre_process(img0, size, resize_mode):
    """Pre-Process input images for yolo detection model"""
    # Padded resize
    img, _, _, _ = letterbox(img0, size, mode=resize_mode)

    # Normalize RGB
    img = img[:, :, ::-1].transpose(2, 0, 1)  # BGR to RGB
    img = np.ascontiguousarray(img, dtype=np.float32)  # uint8 to float32
    img /= 255.0  # 0 - 255 to 0.0 - 1.0
    return img
