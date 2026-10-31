import QtQuick
import QtQuick.Window
import QtMultimedia

Window {
    id: win
    width: 1280
    height: 720
    visible: true
    color: "#0b0d10"
    title: "AnyCam spike 2 - appsink -> QVideoSink"

    VideoOutput {
        id: output
        objectName: "output"
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
    }

    Rectangle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: 16
        width: statsText.implicitWidth + 24
        height: statsText.implicitHeight + 20
        radius: 8
        color: "#cc101418"
        border.color: "#335eead0"
        border.width: 1

        Text {
            id: statsText
            anchors.centerIn: parent
            color: "#d7e3ea"
            font.family: "DejaVu Sans Mono"
            font.pixelSize: 15
            text: backend.statsText
        }
    }

    Rectangle {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 16
        width: 170
        height: 34
        radius: 17
        color: "#cc101418"
        border.color: "#335eead0"
        Text {
            anchors.centerIn: parent
            color: "#5eead0"
            font.family: "DejaVu Sans"
            font.pixelSize: 13
            font.bold: true
            text: "AnyCam  PREVIEW"
        }
    }
}
