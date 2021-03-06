# Person Re-ID system using Detection, Tracking and Person ReID architecture (PCB-RPP)
 

## Preparation
<font face="Times New Roman" size=4>

**Prerequisite: Python 3, Pytorch 0.4+, Tensorflow 1.8+**

## Clone repo
```
git clone https://github.com/hoangtuanvu/Person_Re-Identification.git
```

## Install dependencies
```
cd Person_Re-Identification
pip3 install -r requirements.txt
```

## Download weights for object detection and person re-identification
Download yolov3, yolov3-tiny by running the following script
```
cd detection/weights
./download_weights.sh
```

Download Centernet Version from the following link
https://drive.google.com/open?id=1uiH-SVLqVKEs3AlmBaFFDES5bMD4Jv_F
And then, move the weight to the weight directory of detection module
```
mv model_last_X.pth centernet/models/
```

Download person re-identification from the following link
https://drive.google.com/open?id=1pXNYlCYMSVq_bRvuOqGVBv3-dPgWFjS0

After that, do the following command lines
```
mkdir re_id/weights
mv checkpoint.pth.tar re_id/weights
```

## Run Person Re-Identification App
```
python person_app.py 
    --config-path detection/config/yolov3-tiny.cfg 
    --detection-weight detection/weights/yolov3-tiny.weights 
    -a resnet18
    --tracking-type deep_sort
```

## Convert Images to Videos
Note: This is optional
```
python processor/pre_process.py 
    --images-dir [image_directory_that_contains_folder_of_image_frames]
    --output-dir [output_directory_that_contain_videos_outputs]
    --f 10 #frame_rate
    --c libx264 #encode type
    --img-ext jpg #image extension
    --vid-ext mp4 #video extension
```

## Run Person-Counting App
```
python counting_app.py 
    --config-path detection/config/yolov3.cfg 
    --detection-weight detection/weights/yolov3.weights
    --reid-weights re_id/logs/market-1501/PCB/checkpoint.pth.tar  
    -a resnet18
    --confidence 0.6 
    --nms-thres 0.3 
    --use-resize 
    --img-size 608  
    --counting-use-reid 
    --is-saved
```

## Run Person-Counting by Offline mode
Run with yolov3
```
python counting_objects_runner.py 
    --config-path detection/config/yolov3.cfg 
    --detection-weight detection/weights/yolov3.weights
    -a resnet18
    --confidence 0.5 
    --nms-thres 0.3 
    --img-size 928  
    --inputs [path_to_image_folder]
    --min-shake-point 4 
    --stable-point 10
```

Run with CenterNet
```
python counting_objects_runner.py 
    --confidence 0.4 
    --nms-thres 0.3
    --od-model centernet 
    --load_model [path_to_centernet_ckpt]   
    --inputs [path_to_image_folder]
    --nms
    --keep_res #Optional
    --min-shake-point 4 
    --stable-point 10
```