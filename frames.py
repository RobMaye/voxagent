from pipecat.frames.frames import Frame

class ActionFrame(Frame):
    name = "ActionFrame"

    def __init__(self, action: str):
        super().__init__()
        self.action = action
