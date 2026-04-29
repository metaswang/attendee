class ParticipantSpeechStartStopManager {
    constructor() {
        // Only one active speaker at a time
        this.activeSpeaker = null;
    }

    sendSpeechStartStopEvent(participantId, isSpeechStart, timestamp) {
        window.ws?.sendJson({
            type: 'ParticipantSpeechStartStopEvent',
            participantId: participantId.toString(),
            isSpeechStart: isSpeechStart,
            timestamp: timestamp
        });
    }

    addActiveSpeaker(speakerId) {
        if (this.activeSpeaker === speakerId) {
            return;
        }
        if (this.activeSpeaker) {
            this.sendSpeechStartStopEvent(this.activeSpeaker, false, Date.now());
        }
        this.activeSpeaker = speakerId;
        this.sendSpeechStartStopEvent(this.activeSpeaker, true, Date.now());
    }
}

class DominantSpeakerManager {
    constructor() {
        this.dominantSpeakerStreamId = null;
        this.captionAudioTimes = [];
    }

    getLastSpeakerIdForTimestampMs(timestampMs) {
        // Find the caption audio times that are before timestampMs
        const captionAudioTimesBeforeTimestampMs = this.captionAudioTimes.filter(captionAudioTime => captionAudioTime.timestampMs <= timestampMs);
        if (captionAudioTimesBeforeTimestampMs.length === 0) {
            return null;
        }
        // Return the caption audio time with the highest timestampMs
        return captionAudioTimesBeforeTimestampMs.reduce((max, captionAudioTime) => captionAudioTime.timestampMs > max.timestampMs ? captionAudioTime : max).speakerId;
    }

    addCaptionAudioTime(timestampMs, speakerId) {
        this.captionAudioTimes.push({
            timestampMs: timestampMs,
            speakerId: speakerId
        });
    }

    setDominantSpeakerStreamId(dominantSpeakerStreamId) {
        this.dominantSpeakerStreamId = dominantSpeakerStreamId.toString();
    }

    getDominantSpeaker() {
        return virtualStreamToPhysicalStreamMappingManager.virtualStreamIdToParticipant(this.dominantSpeakerStreamId);
    }
}

const handleAudioTrack = async (event) => {
    let lastAudioFormat = null;  // Track last seen format
    
    try {
      // Create processor to get raw frames
      const processor = new MediaStreamTrackProcessor({ track: event.track });
      const generator = new MediaStreamTrackGenerator({ kind: 'audio' });
      
      // Get readable stream of audio frames
      const readable = processor.readable;
      const writable = generator.writable;
  
      const firstStreamId = event.streams[0]?.id;
      if (!firstStreamId) {
        window.ws?.sendJson({
            type: 'AudioTrackError',
            message: 'No stream ID found for audio track'
        });
        return;
      }
      var userIdForStreamId = null;
      var numAttemptsToMapToUserId = 0;
      
        
      // Transform stream to intercept frames
      const transformStream = new TransformStream({
          async transform(frame, controller) {
              if (!frame) {
                  return;
              }
  
              try {
                  // Check if controller is still active
                  if (controller.desiredSize === null) {
                      frame.close();
                      return;
                  }
  
                  // Copy the audio data
                  const numChannels = frame.numberOfChannels;
                  const numSamples = frame.numberOfFrames;
                  const audioData = new Float32Array(numSamples);
                  
                  // Copy data from each channel
                  // If multi-channel, average all channels together
                  if (numChannels > 1) {
                      // Temporary buffer to hold each channel's data
                      const channelData = new Float32Array(numSamples);
                      
                      // Sum all channels
                      for (let channel = 0; channel < numChannels; channel++) {
                          frame.copyTo(channelData, { planeIndex: channel });
                          for (let i = 0; i < numSamples; i++) {
                              audioData[i] += channelData[i];
                          }
                      }
                      
                      // Average by dividing by number of channels
                      for (let i = 0; i < numSamples; i++) {
                          audioData[i] /= numChannels;
                      }
                  } else {
                      // If already mono, just copy the data
                      frame.copyTo(audioData, { planeIndex: 0 });
                  }
  
                  // console.log('frame', frame)
                  // console.log('audioData', audioData)
  
                  // Check if audio format has changed
                  const currentFormat = {
                      numberOfChannels: 1,
                      originalNumberOfChannels: frame.numberOfChannels,
                      numberOfFrames: frame.numberOfFrames,
                      sampleRate: frame.sampleRate,
                      format: frame.format,
                      duration: frame.duration
                  };
  
                  // If format is different from last seen format, send update
                  if (!lastAudioFormat || 
                      JSON.stringify(currentFormat) !== JSON.stringify(lastAudioFormat)) {
                      lastAudioFormat = currentFormat;
                      ws.sendJson({
                          type: 'AudioFormatUpdate',
                          format: currentFormat
                      });
                  }
  
                  // If the audioData buffer is all zeros, then we don't want to send it
                  if (audioData.every(value => value === 0)) {
                      return;
                  }
  
                  if (!userIdForStreamId) {
                    userIdForStreamId = window.userManager?.getUserIdFromStreamId(firstStreamId);
                    if (userIdForStreamId) {
                        window.ws?.sendJson({
                                type: 'AudioTrackMappedToUserId',
                                trackId: event.track.id,
                                streamId: firstStreamId,
                                userId: userIdForStreamId
                        });
                    }
                    numAttemptsToMapToUserId++;
                    if (numAttemptsToMapToUserId === 1000 && !userIdForStreamId) {
                        window.ws?.sendJson({
                            type: 'AudioTrackMappedToUserIdTimedOut',
                            trackId: event.track.id,
                            streamId: firstStreamId,
                        });
                    }
                  }
                  if (userIdForStreamId)
                    ws.sendPerParticipantAudio(userIdForStreamId, audioData);
                      
                  // Pass through the original frame
                  controller.enqueue(frame);
              } catch (error) {
                  console.error('Error processing frame:', error);
                  frame.close();
              }
          },
          flush() {
              console.log('Transform stream flush called');
          }
      });
  
      // Create an abort controller for cleanup
      const abortController = new AbortController();
  
      try {
          // Connect the streams
          await readable
              .pipeThrough(transformStream)
              .pipeTo(writable, {
                  signal: abortController.signal
              })
              .catch(error => {
                  if (error.name !== 'AbortError') {
                      console.error('Pipeline error:', error);
                  }
              });
      } catch (error) {
          console.error('Stream pipeline error:', error);
          abortController.abort();
      }
  
    } catch (error) {
        console.error('Error setting up audio interceptor:', error);
    }
  };
  

class RTCInterceptor {
    constructor(callbacks) {
        // Store the original RTCPeerConnection
        const originalRTCPeerConnection = window.RTCPeerConnection;
        
        // Store callbacks
        const onPeerConnectionCreate = callbacks.onPeerConnectionCreate || (() => {});
        const onDataChannelCreate = callbacks.onDataChannelCreate || (() => {});
        
        // Override the RTCPeerConnection constructor
        window.RTCPeerConnection = function(...args) {
            // Create instance using the original constructor
            const peerConnection = Reflect.construct(
                originalRTCPeerConnection, 
                args
            );
            
            // Notify about the creation
            onPeerConnectionCreate(peerConnection);
            
            // Override createDataChannel
            const originalCreateDataChannel = peerConnection.createDataChannel.bind(peerConnection);
            peerConnection.createDataChannel = (label, options) => {
                const dataChannel = originalCreateDataChannel(label, options);
                onDataChannelCreate(dataChannel, peerConnection);
                return dataChannel;
            };
            
            return peerConnection;
        };
    }
}

new RTCInterceptor({
    onPeerConnectionCreate: (peerConnection) => {
        console.log('New RTCPeerConnection created:', peerConnection);

        peerConnection.addEventListener('track', (event) => {
            console.log('New track:', {
                trackId: event.track.id,
                trackKind: event.track.kind,
                streams: event.streams,
            });

            window.ws?.sendJson({
                type: 'WebRTCTrackStarted',
                trackId: event.track.id,
                trackKind: event.track.kind,
                streams: event.streams?.map(stream => stream?.id),
            });

            // We need to capture every audio track in the meeting,
            // but we don't need to do anything with the video tracks
            if (event.track.kind === 'audio') {
                window.mixedAudioStreamManager?.addAudioTrackFromTrackEvent(event);
                if (window.initialData.sendPerParticipantAudio) {
                    handleAudioTrack(event);
                }
                window.ws?.tryStartMediaSendingRecorders?.();
            } else if (event.track.kind === 'video') {
                window.styleManager?.addVideoTrack?.(event);
                window.ws?.tryStartMediaSendingRecorders?.();
            }
        });
    },
});

class MixedAudioStreamManager {
    constructor() {
        this.audioTracks = [];
        this.meetingAudioStream = null;
        this.audioTracksToBeAdded = [];
        this.audioContext = null;
        this.destination = null;
        this.seenTrackIds = new Set();
    }


    addAudioStream(audioStream) {
        const track = audioStream.getAudioTracks()[0];
        if (track) {
            this.addAudioTrack(track);
        }
    }

    addAudioTrackFromTrackEvent(trackEvent) {
        if (!trackEvent.track)
            return;
        const firstStreamId = trackEvent.streams[0]?.id;
        // streamId must contain +CS+ in it, which means it's from Zoom, not from a voice agent.
        if (!firstStreamId?.includes('+CS+')) {
            window.ws?.sendJson({
                type: 'AudioTrackNotAddedToMeetingAudioStream',
                trackId: trackEvent.track.id,
                streams: trackEvent.streams?.map(stream => stream?.id),
            });
            return;
        }
        window.ws?.sendJson({
            type: 'AudioTrackAddedToMeetingAudioStream',
            trackId: trackEvent.track.id,
            streams: trackEvent.streams?.map(stream => stream?.id),
        });
        this.addAudioTrack(trackEvent.track);
    }

    addAudioTrack(track) {
        if (!track || this.seenTrackIds.has(track.id)) {
            return;
        }

        // If start() already ran, patch the new track into the existing mix.
        if (this.audioContext && this.destination) {
            const mediaStream = new MediaStream([track]);
            const source = this.audioContext.createMediaStreamSource(mediaStream);
            source.connect(this.destination);
            this.seenTrackIds.add(track.id);
        }
        else {
            this.audioTracksToBeAdded.push(track);
        }
        window.ws?.tryStartMediaSendingRecorders?.();
    }

    createStream() {
        if (this.meetingAudioStream)
            return;
        this.audioContext = new AudioContext({ sampleRate: 48000 });
        this.destination = this.audioContext.createMediaStreamDestination();

        this.audioTracksToBeAdded.forEach(track => this.addAudioTrack(track));

        this.meetingAudioStream = this.destination.stream;

        // Create a source from the destination's stream so that it actually plays
        this.audioContext.createMediaStreamSource(this.destination.stream);

        window.ws?.emitDiagnosticEvent?.('MeetingAudioStreamCreated', {
            message: 'Meeting audio stream created',
        });
    }

    getMeetingAudioStream() {
        this.createStream();
        return this.meetingAudioStream;
    }

    hasConnectedMeetingAudioTracks() {
        return this.seenTrackIds.size > 0;
    }
}

// Style manager
class StyleManager {
    constructor() {
        this.started = false;
        this.videoTracks = new Map();
    }

    async start() {
        console.log('StyleManager start');

        this.started = true;

        if (window.zoomInitialData.modifyDomForVideoRecording) {
            this.onlyShowSubsetofZoomUI();
        }
    }
    
    getMeetingAudioStream() {
        if (!this.started)
            return null;
        return window.mixedAudioStreamManager?.getMeetingAudioStream();
    }

    hasMeetingAudioInputForRecording() {
        if (!this.started) {
            return false;
        }
        return !!window.mixedAudioStreamManager?.hasConnectedMeetingAudioTracks?.();
    }

    addVideoTrack(trackEvent) {
        const track = trackEvent?.track;
        const streamId = trackEvent?.streams?.[0]?.id;
        if (!track || !streamId) {
            return;
        }
        this.videoTracks.set(track.id, {
            track,
            streamId,
            firstSeenAt: Date.now(),
        });
        track.addEventListener('ended', () => {
            this.videoTracks.delete(track.id);
        }, { once: true });
        window.ws?.emitDiagnosticEvent?.('RecordingVideoSourceSelected', {
            track_id: track.id,
            stream_id: streamId,
        });
        window.ws?.tryStartMediaSendingRecorders?.();
    }

    getVideoTrackForRecording() {
        const liveTracks = Array.from(this.videoTracks.values()).filter((item) => item.track?.readyState === 'live');
        if (!liveTracks.length) {
            return null;
        }
        return liveTracks.sort((a, b) => b.firstSeenAt - a.firstSeenAt)[0].track;
    }

    getRenderableElementForRecording() {
        if (!this.started) {
            return null;
        }

        const recordingRoot = this.mainElement || document.querySelector('#video-pip-container');
        if (!recordingRoot) {
            return null;
        }

        const candidates = Array.from(recordingRoot.querySelectorAll('video, canvas')).filter((element) => {
            if (!element.isConnected) {
                return false;
            }
            const rect = element.getBoundingClientRect();
            return rect.width >= 16 && rect.height >= 16;
        });

        if (!candidates.length) {
            return null;
        }

        const rankedCandidates = candidates.map((element) => {
            const isVideo = element.tagName === 'VIDEO';
            const width = isVideo ? element.videoWidth : element.width;
            const height = isVideo ? element.videoHeight : element.height;
            const rect = element.getBoundingClientRect();
            const isRenderable = isVideo
                ? element.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && width > 0 && height > 0
                : width > 0 && height > 0;
            return {
                element,
                isRenderable,
                area: rect.width * rect.height,
            };
        }).filter((item) => item.isRenderable);

        if (!rankedCandidates.length) {
            return null;
        }

        return rankedCandidates.sort((a, b) => b.area - a.area)[0].element;
    }

    async stop() {
        console.log('StyleManager stop');
        if (window.zoomInitialData.modifyDomForVideoRecording) {
            this.showAllOfZoomUI();
        }
    }

    onlyShowSubsetofZoomUI() {
        try {
            // Find the main element that contains all the video elements
            this.mainElement = document.querySelector('#video-pip-container');
            if (!this.mainElement) {
                console.error('No #video-pip-container element found in the DOM');
                window.ws.sendJson({
                    type: 'Error',
                    message: 'No #video-pip-container element found in the DOM'
                });
                return;
            }

            const ancestors = [];
            let parent = this.mainElement.parentElement;
            while (parent) {
                ancestors.push(parent);
                parent = parent.parentElement;
            }
            
            // Hide all elements except main, its ancestors, and its descendants
            document.querySelectorAll('body *').forEach(element => {
                if (element !== this.mainElement && 
                    !ancestors.includes(element) && 
                    !this.mainElement.contains(element)) {
                    element.style.display = 'none';
                }
            });
        } catch (error) {
            console.error('Error in onlyShowSubsetofZoomUI:', error);
            window.ws.sendJson({
                type: 'Error',
                message: 'Error in onlyShowSubsetofZoomUI: ' + error.message
            });
        }
    }


    showAllOfZoomUI() {
        // Restore all elements that were hidden by onlyShowSubsetofZoomUI
        document.querySelectorAll('body *').forEach(element => {
            if (element.style.display === 'none') {
                // Only reset display property if we set it to 'none'
                // We can check if the element is a direct child of body or not in main/ancestors
                const isInMainTree = this.mainElement && 
                    (this.mainElement === element || 
                     this.mainElement.contains(element) || 
                     element.contains(this.mainElement));
                
                if (!isInMainTree) {
                    // Reset the display property to its default or empty string
                    // This will restore the element's original display value
                    element.style.display = '';
                }
            }
        });
        
        console.log('Restored all hidden elements to their original display values');
    }
}

// Websocket client
class WebSocketClient {
    // Message types
    static MESSAGE_TYPES = {
        JSON: 1,
        VIDEO: 2,
        AUDIO: 3,
        ENCODED_MP4_CHUNK: 4,
        PER_PARTICIPANT_AUDIO: 5,
        ENCODED_AUDIO_CHUNK: 6
    };

    constructor() {
        const url = `ws://localhost:${window.initialData.websocketPort}`;
        console.log('WebSocketClient url', url);
        this.ws = new WebSocket(url);
        this.ws.binaryType = 'arraybuffer';
        
        this.ws.onopen = () => {
            console.log('WebSocket Connected');
        };
        
        this.ws.onmessage = (event) => {
            this.handleMessage(event.data);
        };
        
        this.ws.onerror = (error) => {
            console.error('WebSocket Error:', error);
        };
        
        this.ws.onclose = () => {
            console.log('WebSocket Disconnected');
        };

        this.mediaSendingEnabled = false;
        this.audioChunkRecorder = null;
        this.audioChunkFlushInterval = null;
        this.videoChunkRecorder = null;
        this.videoChunkFlushInterval = null;
        this.videoChunkCanvas = null;
        this.videoChunkCanvasCtx = null;
        this.videoChunkVideoElement = null;
        this.videoChunkAnimationFrame = null;
        this.videoChunkDomSourceElement = null;
        this.videoChunkDomSourceKey = null;
        this.videoChunkSourceTrackId = null;
        this.videoChunkSourceTrackClone = null;
        this.videoChunkSourceUnavailableReason = null;
        this.encodedVideoChunkCount = 0;
        this.encodedVideoChunkZeroSizeCount = 0;
        this.videoChunkRequestCount = 0;
        this.encodedAudioChunkCount = 0;
        this.audioChunkZeroSizeCount = 0;
        this.audioChunkRequestCount = 0;
    }

    async enableMediaSending() {
        this.mediaSendingEnabled = true;
        await window.styleManager.start();
        this.tryStartMediaSendingRecorders();
    }

    async disableMediaSending() {
        await this.stopVideoChunkRecording();
        await this.stopAudioChunkRecording();
        window.styleManager.stop();
        // Give the media recorder a bit of time to send the final data
        await new Promise(resolve => setTimeout(resolve, 2000));
        this.mediaSendingEnabled = false;
    }

    handleMessage(data) {
        const view = new DataView(data);
        const messageType = view.getInt32(0, true); // true for little-endian
        
        // Handle different message types
        switch (messageType) {
            case WebSocketClient.MESSAGE_TYPES.JSON:
                const jsonData = new TextDecoder().decode(new Uint8Array(data, 4));
                console.log('Received JSON message:', JSON.parse(jsonData));
                break;
            // Add future message type handlers here
            default:
                console.warn('Unknown message type:', messageType);
        }
    }

    sendJson(data) {
        if (this.ws.readyState !== WebSocket.OPEN) {
            console.error('WebSocket is not connected');
            return;
        }

        try {
            // Convert JSON to string then to Uint8Array
            const jsonString = JSON.stringify(data);
            const jsonBytes = new TextEncoder().encode(jsonString);
            
            // Create final message: type (4 bytes) + json data
            const message = new Uint8Array(4 + jsonBytes.length);
            
            // Set message type (1 for JSON)
            new DataView(message.buffer).setInt32(0, WebSocketClient.MESSAGE_TYPES.JSON, true);
            
            // Copy JSON data after type
            message.set(jsonBytes, 4);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            console.error('Error sending WebSocket message:', error);
            console.error('Message data:', data);
        }
    }

    emitDiagnosticEvent(type, payload = {}) {
        this.sendJson({
            type,
            ...payload,
        });
    }

    tryStartMediaSendingRecorders() {
        if (!this.mediaSendingEnabled) {
            return;
        }
        if (window.initialData.sendEncodedVideoChunks && (!this.videoChunkRecorder || this.videoChunkRecorder.state === 'inactive')) {
            this.startVideoChunkRecording();
        }
        if (window.initialData.sendEncodedAudioChunks && (!this.audioChunkRecorder || this.audioChunkRecorder.state === 'inactive')) {
            this.startAudioChunkRecording();
        }
    }

    sendClosedCaptionUpdate(item) {
        if (!this.mediaSendingEnabled)
            return;

        this.sendJson({
            type: 'CaptionUpdate',
            caption: item
        });
    }

    sendEncodedAudioChunk(encodedAudioData) {
        if (this.ws.readyState !== WebSocket.OPEN || !this.mediaSendingEnabled) {
            return;
        }
        try {
            const headerBuffer = new ArrayBuffer(4);
            const headerView = new DataView(headerBuffer);
            headerView.setInt32(0, WebSocketClient.MESSAGE_TYPES.ENCODED_AUDIO_CHUNK, true);
            this.ws.send(new Blob([headerBuffer, encodedAudioData]));
        } catch (error) {
            console.error('Error sending WebSocket audio chunk:', error);
        }
    }

    sendEncodedMP4Chunk(encodedMP4Data) {
        if (this.ws.readyState !== WebSocket.OPEN || !this.mediaSendingEnabled) {
            return;
        }
        try {
            const headerBuffer = new ArrayBuffer(4);
            const headerView = new DataView(headerBuffer);
            headerView.setInt32(0, WebSocketClient.MESSAGE_TYPES.ENCODED_MP4_CHUNK, true);
            this.ws.send(new Blob([headerBuffer, encodedMP4Data]));
        } catch (error) {
            console.error('Error sending WebSocket video chunk:', error);
        }
    }

    getMeetingAudioTrackForRecording() {
        const meetingAudioStream = window.styleManager?.getMeetingAudioStream?.();
        if (!meetingAudioStream) {
            return null;
        }

        const audioTracks = meetingAudioStream.getAudioTracks?.() || [];
        return audioTracks.find((track) => track.readyState === 'live') || audioTracks[0] || null;
    }

    getPreferredVideoChunkMimeType() {
        const preferredMimeTypes = [
            'video/webm;codecs=vp9,opus',
            'video/webm;codecs=vp8,opus',
            'video/webm',
            'video/mp4;codecs=avc1.42E01E,mp4a.40.2',
            'video/mp4',
        ];
        return preferredMimeTypes.find((mime) => window.MediaRecorder && MediaRecorder.isTypeSupported(mime)) || '';
    }

    ensureVideoChunkCanvas() {
        if (!this.videoChunkCanvas) {
            this.videoChunkCanvas = document.createElement('canvas');
            this.videoChunkCanvas.width = window.initialData.videoFrameWidth || 1280;
            this.videoChunkCanvas.height = window.initialData.videoFrameHeight || 720;
            this.videoChunkCanvasCtx = this.videoChunkCanvas.getContext('2d');
        }
        if (!this.videoChunkVideoElement) {
            this.videoChunkVideoElement = document.createElement('video');
            this.videoChunkVideoElement.autoplay = true;
            this.videoChunkVideoElement.muted = true;
            this.videoChunkVideoElement.playsInline = true;
        }
    }

    stopVideoChunkCanvasLoop() {
        if (this.videoChunkAnimationFrame) {
            cancelAnimationFrame(this.videoChunkAnimationFrame);
            this.videoChunkAnimationFrame = null;
        }
    }

    cleanupVideoChunkSourceTrack() {
        if (this.videoChunkSourceTrackClone) {
            try {
                this.videoChunkSourceTrackClone.stop();
            } catch (error) {
                console.warn('Video chunk source track cleanup failed', error);
            }
            this.videoChunkSourceTrackClone = null;
        }
        this.videoChunkSourceTrackId = null;
        if (this.videoChunkVideoElement) {
            this.videoChunkVideoElement.srcObject = null;
        }
    }

    cleanupVideoChunkDomSourceElement() {
        this.videoChunkDomSourceElement = null;
        this.videoChunkDomSourceKey = null;
    }

    emitVideoChunkSourceUnavailable(reason, payload = {}) {
        if (this.videoChunkSourceUnavailableReason === reason) {
            return;
        }
        this.videoChunkSourceUnavailableReason = reason;
        window.ws?.emitDiagnosticEvent?.('RecordingVideoSourceUnavailable', {
            reason,
            ...payload,
        });
    }

    syncVideoChunkDomSourceElement() {
        this.ensureVideoChunkCanvas();
        const nextElement = window.styleManager?.getRenderableElementForRecording?.();
        if (!nextElement) {
            this.cleanupVideoChunkDomSourceElement();
            return false;
        }

        const nextKey = [
            nextElement.tagName,
            nextElement.id || '',
            nextElement.className || '',
            nextElement.currentSrc || '',
            nextElement.srcObject?.id || '',
        ].join('|');

        if (this.videoChunkDomSourceElement === nextElement && this.videoChunkDomSourceKey === nextKey) {
            this.videoChunkSourceUnavailableReason = null;
            return true;
        }

        this.cleanupVideoChunkSourceTrack();
        this.videoChunkDomSourceElement = nextElement;
        this.videoChunkDomSourceKey = nextKey;
        this.videoChunkSourceUnavailableReason = null;
        window.ws?.emitDiagnosticEvent?.('RecordingVideoSourceSelected', {
            source_type: 'dom_element',
            tag_name: nextElement.tagName,
            id: nextElement.id || null,
            class_name: nextElement.className || null,
        });
        return true;
    }

    syncVideoChunkSourceTrack() {
        this.ensureVideoChunkCanvas();
        if (this.syncVideoChunkDomSourceElement()) {
            return;
        }
        const nextTrack = window.styleManager?.getVideoTrackForRecording?.();
        if (!nextTrack || nextTrack.readyState !== 'live') {
            this.cleanupVideoChunkSourceTrack();
            this.emitVideoChunkSourceUnavailable('missing_track', {
                has_track: !!nextTrack,
                ready_state: nextTrack?.readyState || null,
            });
            return;
        }
        if (this.videoChunkSourceTrackId === nextTrack.id) {
            this.videoChunkSourceUnavailableReason = null;
            return;
        }
        this.cleanupVideoChunkDomSourceElement();
        this.cleanupVideoChunkSourceTrack();
        this.videoChunkSourceTrackClone = nextTrack.clone();
        this.videoChunkSourceTrackId = nextTrack.id;
        this.videoChunkSourceUnavailableReason = null;
        this.videoChunkVideoElement.srcObject = new MediaStream([this.videoChunkSourceTrackClone]);
        this.videoChunkVideoElement.play().catch((error) => {
            console.warn('Video chunk preview play failed', error);
        });
    }

    drawVideoChunkFrame = () => {
        if (!this.mediaSendingEnabled) {
            this.stopVideoChunkCanvasLoop();
            return;
        }
        this.syncVideoChunkSourceTrack();
        const ctx = this.videoChunkCanvasCtx;
        const canvas = this.videoChunkCanvas;
        const video = this.videoChunkVideoElement;
        if (!ctx || !canvas) {
            return;
        }

        const domSource = this.videoChunkDomSourceElement;
        if (domSource?.isConnected) {
            const isVideo = domSource.tagName === 'VIDEO';
            const sourceWidth = isVideo ? domSource.videoWidth : domSource.width;
            const sourceHeight = isVideo ? domSource.videoHeight : domSource.height;
            const isRenderable = isVideo
                ? domSource.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && sourceWidth > 0 && sourceHeight > 0
                : sourceWidth > 0 && sourceHeight > 0;
            if (isRenderable) {
                if (canvas.width !== sourceWidth || canvas.height !== sourceHeight) {
                    canvas.width = sourceWidth;
                    canvas.height = sourceHeight;
                }
                ctx.drawImage(domSource, 0, 0, canvas.width, canvas.height);
                this.videoChunkAnimationFrame = requestAnimationFrame(this.drawVideoChunkFrame);
                return;
            }
        }

        if (video && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth && video.videoHeight) {
            if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
                canvas.width = video.videoWidth;
                canvas.height = video.videoHeight;
            }
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        } else {
            ctx.fillStyle = 'black';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
        }

        this.videoChunkAnimationFrame = requestAnimationFrame(this.drawVideoChunkFrame);
    };

    async startVideoChunkRecording() {
        if (this.videoChunkRecorder && this.videoChunkRecorder.state !== 'inactive') {
            return;
        }
        if (!window.MediaRecorder) {
            console.warn('MediaRecorder is not available for video chunk recording');
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'video',
                state: 'failed',
                reason: 'media_recorder_unavailable',
            });
            return;
        }

        this.ensureVideoChunkCanvas();
        this.syncVideoChunkSourceTrack();
        const audioTrack = this.getMeetingAudioTrackForRecording();
        const hasMeetingAudioInput = window.styleManager?.hasMeetingAudioInputForRecording?.();
        if ((!audioTrack || !hasMeetingAudioInput) && !window.initialData.sendEncodedAudioChunks) {
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'video',
                state: 'failed',
                reason: !audioTrack ? 'missing_audio_stream' : 'missing_audio_source',
            });
            return;
        }
        this.emitDiagnosticEvent('RecordingChunkRecorderState', {
            kind: 'video',
            state: 'starting',
            has_audio_track: !!audioTrack,
            has_audio_source: !!hasMeetingAudioInput,
            has_dom_source: !!this.videoChunkDomSourceElement,
            has_video_track: !!this.videoChunkSourceTrackClone,
            video_track_id: this.videoChunkSourceTrackId || null,
        });

        const recorderStream = this.videoChunkCanvas.captureStream(30);
        if (audioTrack) {
            recorderStream.addTrack(audioTrack.clone());
        }

        const selectedMimeType = this.getPreferredVideoChunkMimeType();
        try {
            this.videoChunkRecorder = selectedMimeType
                ? new MediaRecorder(recorderStream, { mimeType: selectedMimeType })
                : new MediaRecorder(recorderStream);
        } catch (error) {
            console.warn('Video chunk recorder construction failed', error);
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'video',
                state: 'failed',
                reason: 'media_recorder_constructor_failed',
                message: error?.message || String(error),
            });
            return;
        }
        const videoChunkMimeType = this.videoChunkRecorder.mimeType || selectedMimeType || 'video/webm';
        this.emitDiagnosticEvent('RecordingChunkFormat', {
            kind: 'video',
            mimeType: videoChunkMimeType,
            extension: videoChunkMimeType.includes('mp4') ? 'mp4' : 'webm',
        });
        this.videoChunkRecorder.ondataavailable = (event) => {
            this.emitDiagnosticEvent('EncodedMediaChunk', {
                kind: 'video',
                byte_length: event?.data?.size || 0,
                mime_type: event?.data?.type || selectedMimeType || this.videoChunkRecorder?.mimeType || null,
            });
            if (event.data && event.data.size > 0) {
                this.encodedVideoChunkCount += 1;
                this.sendEncodedMP4Chunk(event.data);
                return;
            }
            this.encodedVideoChunkZeroSizeCount += 1;
        };
        this.videoChunkRecorder.onerror = (event) => {
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'video',
                state: 'failed',
                reason: 'media_recorder_error',
                message: event?.error?.message || event?.message || String(event),
            });
        };
        this.videoChunkRecorder.onstart = () => {
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'video',
                state: 'started',
                mime_type: this.videoChunkRecorder?.mimeType || selectedMimeType || 'video/webm',
                has_audio_track: !!audioTrack,
                has_audio_source: !!hasMeetingAudioInput,
                has_dom_source: !!this.videoChunkDomSourceElement,
                has_video_track: !!this.videoChunkSourceTrackClone,
                video_track_id: this.videoChunkSourceTrackId || null,
            });
        };
        this.videoChunkRecorder.onstop = () => {
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'video',
                state: 'stopped',
                encoded_video_chunk_count: this.encodedVideoChunkCount,
                zero_size_chunk_count: this.encodedVideoChunkZeroSizeCount,
                request_count: this.videoChunkRequestCount,
            });
        };
        this.stopVideoChunkCanvasLoop();
        this.videoChunkAnimationFrame = requestAnimationFrame(this.drawVideoChunkFrame);
        try {
            this.videoChunkRecorder.start();
        } catch (error) {
            console.warn('Video chunk recorder start failed', error);
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'video',
                state: 'failed',
                reason: 'media_recorder_start_failed',
                message: error?.message || String(error),
            });
            this.videoChunkRecorder = null;
            return;
        }
        this.videoChunkFlushInterval = setInterval(() => {
            try {
                if (this.videoChunkRecorder?.state === 'recording') {
                    this.videoChunkRequestCount += 1;
                    this.videoChunkRecorder.requestData();
                }
            } catch (error) {
                console.warn('Video chunk recorder requestData failed', error);
                this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                    kind: 'video',
                    state: 'failed',
                    reason: 'request_data_failed',
                    message: error?.message || String(error),
                });
            }
        }, window.initialData.recordingChunkIntervalMs || 5000);
    }

    async stopVideoChunkRecording() {
        if (this.videoChunkFlushInterval) {
            clearInterval(this.videoChunkFlushInterval);
            this.videoChunkFlushInterval = null;
        }
        if (!this.videoChunkRecorder || this.videoChunkRecorder.state === 'inactive') {
            this.stopVideoChunkCanvasLoop();
            this.cleanupVideoChunkDomSourceElement();
            this.cleanupVideoChunkSourceTrack();
            this.videoChunkSourceUnavailableReason = null;
            return;
        }

        await new Promise((resolve) => {
            const recorder = this.videoChunkRecorder;
            recorder.addEventListener('stop', () => resolve(), { once: true });
            try {
                recorder.requestData();
            } catch (error) {
                console.warn('Final video chunk requestData failed', error);
            }
            recorder.stop();
        });
        this.videoChunkRecorder = null;
        this.stopVideoChunkCanvasLoop();
        this.cleanupVideoChunkDomSourceElement();
        this.cleanupVideoChunkSourceTrack();
        this.videoChunkSourceUnavailableReason = null;
    }

    async startAudioChunkRecording() {
        if (this.audioChunkRecorder && this.audioChunkRecorder.state !== 'inactive') {
            return;
        }
        const audioStream = window.styleManager?.getMeetingAudioStream?.();
        if (!audioStream || audioStream.getAudioTracks().length === 0) {
            console.warn('No meeting audio stream available for audio chunk recording');
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'audio',
                state: 'failed',
                reason: 'missing_audio_stream',
            });
            return;
        }
        const preferredMimeTypes = ['audio/webm;codecs=opus', 'audio/webm'];
        const selectedMimeType = preferredMimeTypes.find((mime) => window.MediaRecorder && MediaRecorder.isTypeSupported(mime));
        this.emitDiagnosticEvent('RecordingChunkRecorderState', {
            kind: 'audio',
            state: 'starting',
            has_audio_stream: !!audioStream,
            track_count: audioStream.getAudioTracks().length,
        });
        try {
            this.audioChunkRecorder = selectedMimeType ? new MediaRecorder(audioStream, { mimeType: selectedMimeType }) : new MediaRecorder(audioStream);
        } catch (error) {
            console.warn('Audio chunk recorder construction failed', error);
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'audio',
                state: 'failed',
                reason: 'media_recorder_constructor_failed',
                message: error?.message || String(error),
            });
            return;
        }
        const audioChunkMimeType = this.audioChunkRecorder.mimeType || selectedMimeType || 'audio/webm';
        this.emitDiagnosticEvent('RecordingChunkFormat', {
            kind: 'audio',
            mimeType: audioChunkMimeType,
            extension: audioChunkMimeType.includes('mp4') ? 'm4a' : 'webm',
        });
        this.audioChunkRecorder.onstart = () => {
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'audio',
                state: 'started',
                mime_type: this.audioChunkRecorder?.mimeType || selectedMimeType || 'audio/webm',
                track_count: audioStream.getAudioTracks().length,
            });
        };
        this.audioChunkRecorder.onstop = () => {
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'audio',
                state: 'stopped',
                encoded_audio_chunk_count: this.encodedAudioChunkCount,
                zero_size_chunk_count: this.audioChunkZeroSizeCount,
                request_count: this.audioChunkRequestCount,
            });
        };
        this.audioChunkRecorder.onerror = (event) => {
            this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                kind: 'audio',
                state: 'failed',
                reason: 'media_recorder_error',
                message: event?.error?.message || event?.message || String(event),
            });
        };
        this.audioChunkRecorder.ondataavailable = (event) => {
            this.emitDiagnosticEvent('EncodedMediaChunk', {
                kind: 'audio',
                byte_length: event?.data?.size || 0,
                mime_type: event?.data?.type || selectedMimeType || this.audioChunkRecorder?.mimeType || null,
            });
            if (event.data && event.data.size > 0) {
                this.encodedAudioChunkCount += 1;
                this.sendEncodedAudioChunk(event.data);
                return;
            }
        };
        this.audioChunkRecorder.start();
        this.audioChunkFlushInterval = setInterval(() => {
            try {
                if (this.audioChunkRecorder?.state === 'recording') {
                    this.audioChunkRequestCount += 1;
                    this.audioChunkRecorder.requestData();
                }
            } catch (error) {
                console.warn('Audio chunk recorder requestData failed', error);
                this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                    kind: 'audio',
                    state: 'failed',
                    reason: 'request_data_failed',
                    message: error?.message || String(error),
                });
            }
        }, window.initialData.recordingChunkIntervalMs || 5000);
    }

    async stopAudioChunkRecording() {
        if (this.audioChunkFlushInterval) {
            clearInterval(this.audioChunkFlushInterval);
            this.audioChunkFlushInterval = null;
        }
        if (!this.audioChunkRecorder || this.audioChunkRecorder.state === 'inactive') {
            return;
        }
        await new Promise((resolve) => {
            const recorder = this.audioChunkRecorder;
            recorder.addEventListener('stop', () => resolve(), { once: true });
            try {
                this.audioChunkRequestCount += 1;
                recorder.requestData();
            } catch (error) {
                console.warn('Final audio chunk requestData failed', error);
                this.emitDiagnosticEvent('RecordingChunkRecorderState', {
                    kind: 'audio',
                    state: 'failed',
                    reason: 'final_request_data_failed',
                    message: error?.message || String(error),
                });
            }
            recorder.stop();
        });
        this.audioChunkRecorder = null;
    }

    sendPerParticipantAudio(participantId, audioData) {
        if (this.ws.readyState !== WebSocket.OPEN) {
        console.error('WebSocket is not connected for per participant audio send', this.ws.readyState);
        return;
        }

        if (!this.mediaSendingEnabled) {
        return;
        }

        try {
            // Convert participantId to UTF-8 bytes
            const participantIdBytes = new TextEncoder().encode(participantId);
            
            // Create final message: type (4 bytes) + participantId length (1 byte) + 
            // participantId bytes + audio data
            const message = new Uint8Array(4 + 1 + participantIdBytes.length + audioData.buffer.byteLength);
            const dataView = new DataView(message.buffer);
            
            // Set message type (5 for PER_PARTICIPANT_AUDIO)
            dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.PER_PARTICIPANT_AUDIO, true);
            
            // Set participantId length as uint8 (1 byte)
            dataView.setUint8(4, participantIdBytes.length);
            
            // Copy participantId bytes
            message.set(participantIdBytes, 5);
            
            // Copy audio data after type, length and participantId
            message.set(new Uint8Array(audioData.buffer), 5 + participantIdBytes.length);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            console.error('Error sending WebSocket audio message:', error);
        }
    }

    sendMixedAudio(timestamp, audioData) {
        if (this.ws.readyState !== WebSocket.OPEN) {
            console.error('WebSocket is not connected for audio send', this.ws.readyState);
            return;
        }

        if (!this.mediaSendingEnabled) {
            return;
        }

        try {
            // Create final message: type (4 bytes) + audio data
            const message = new Uint8Array(4 + audioData.buffer.byteLength);
            const dataView = new DataView(message.buffer);
            
            // Set message type (3 for AUDIO)
            dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.AUDIO, true);
            
            // Copy audio data after type
            message.set(new Uint8Array(audioData.buffer), 4);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            console.error('Error sending WebSocket audio message:', error);
        }
    }
}

class UserManager {
    constructor(ws) {
        this.allUsersMap = new Map();
        this.currentUsersMap = new Map();
        this.deviceOutputMap = new Map();

        this.ws = ws;
    }

    getUserIdFromStreamId(streamId) {
        const decoded = decodeURIComponent(streamId);
        const match = decoded.match(/^(\d+)\+/);
        if (match) {
            const rawId = Number(match[1]);
            const participantId = rawId >> 10 << 10;
            // Check if this exists in the current users map
            if (this.currentUsersMap.has(participantId.toString())) {
                return participantId.toString();
            }
            return null;
        }
        return null;
    }

    getUserByDeviceId(deviceId) {
        return this.allUsersMap.get(deviceId);
    }

    // constants for meeting status
    MEETING_STATUS = {
        IN_MEETING: 1,
        NOT_IN_MEETING: 6
    }

    getCurrentUsersInMeeting() {
        return Array.from(this.currentUsersMap.values()).filter(user => user.status === this.MEETING_STATUS.IN_MEETING);
    }

    getCurrentUsersInMeetingWhoAreScreenSharing() {
        return this.getCurrentUsersInMeeting().filter(user => user.parentDeviceId);
    }

    convertUser(zoomUser) {
        return {
            deviceId: zoomUser.userId.toString(),
            displayName: zoomUser.userName,
            fullName: zoomUser.userName,
            profile: '',
            status: zoomUser.state,
            isHost: zoomUser.isHost,
            humanized_status: zoomUser.state === "active" ? "in_meeting" : "not_in_meeting",
            isCurrentUser: zoomUser.self
        };
    }

    singleUserSynced(user) {
      const convertedUser = this.convertUser(user);
      console.log('singleUserSynced called w', convertedUser);
      // Create array with new user and existing users, then filter for unique deviceIds
      // keeping the first occurrence (new user takes precedence)
      const allUsers = [...this.currentUsersMap.values(), convertedUser];
      console.log('allUsers', allUsers);
      const uniqueUsers = Array.from(
        new Map(allUsers.map(singleUser => [singleUser.deviceId, singleUser])).values()
      );
      this.newUsersListSynced(uniqueUsers);
    }

    newUsersListSynced(newUsersList) {
        console.log('newUsersListSynced called w', newUsersList);
        // Get the current user IDs before updating
        const previousUserIds = new Set(this.currentUsersMap.keys());
        const newUserIds = new Set(newUsersList.map(user => user.deviceId));
        const updatedUserIds = new Set([])

        // Update all users map
        for (const user of newUsersList) {
            if (previousUserIds.has(user.deviceId) && JSON.stringify(this.currentUsersMap.get(user.deviceId)) !== JSON.stringify(user)) {
                updatedUserIds.add(user.deviceId);
            }

            this.allUsersMap.set(user.deviceId, {
                deviceId: user.deviceId,
                displayName: user.displayName,
                fullName: user.fullName,
                profile: user.profile,
                status: user.status,
                humanized_status: user.humanized_status,
                parentDeviceId: user.parentDeviceId,
                isCurrentUser: user.isCurrentUser,
                isHost: user.isHost
            });
        }

        // Calculate new, removed, and updated users
        const newUsers = newUsersList.filter(user => !previousUserIds.has(user.deviceId));
        const removedUsers = Array.from(previousUserIds)
            .filter(id => !newUserIds.has(id))
            .map(id => this.currentUsersMap.get(id));

        if (removedUsers.length > 0) {
            console.log('removedUsers', removedUsers);
        }

        // Clear current users map and update with new list
        this.currentUsersMap.clear();
        for (const user of newUsersList) {
            this.currentUsersMap.set(user.deviceId, {
                deviceId: user.deviceId,
                displayName: user.displayName,
                fullName: user.fullName,
                profilePicture: user.profilePicture,
                status: user.status,
                humanized_status: user.humanized_status,
                parentDeviceId: user.parentDeviceId,
                isCurrentUser: user.isCurrentUser,
                isHost: user.isHost
            });
        }

        const updatedUsers = Array.from(updatedUserIds).map(id => this.currentUsersMap.get(id));

        if (newUsers.length > 0 || removedUsers.length > 0 || updatedUsers.length > 0) {
            this.ws.sendJson({
                type: 'UsersUpdate',
                newUsers: newUsers,
                removedUsers: removedUsers,
                updatedUsers: updatedUsers
            });
        }
    }
}

// This code intercepts the connect method on the AudioNode class
// When something is connected to the speaker the underlying track is added to our styleManager
// so that it can be aggregated into a stream representing the meeting audio
(() => {
    const origConnect = AudioNode.prototype.connect;
  
    AudioNode.prototype.connect = function(target, ...rest) {

      // Only intercept connections directly to the speakers. The target !== window.botOutputManager?.getAudioContextDestination() condition is to avoid capturing the bots output 
      if (target instanceof AudioDestinationNode && target !== window.botOutputManager?.getAudioContextDestination()) {
        const ctx = this.context;
        // Create a single tee per context
        if (!ctx.__captureTee) {
        try{
          const tee = ctx.createGain();
          const tap = ctx.createMediaStreamDestination();
          origConnect.call(tee, ctx.destination); // keep normal playback
          origConnect.call(tee, tap);             // capture
          ctx.__captureTee = { tee, tap };
          const capturedStream = tap.stream;
          if (capturedStream)
            window.mixedAudioStreamManager.addAudioStream(capturedStream);
        }
        catch (error) {
            console.error('Error in AudioNodeInterceptor:', error);
        }
        }
  
        // Reroute to the tee instead of the destination
        return origConnect.call(this, ctx.__captureTee.tee, ...rest);
      }
  
      return origConnect.call(this, target, ...rest);
    };
  })();

const ws = new WebSocketClient();
window.ws = ws;
const dominantSpeakerManager = new DominantSpeakerManager();
window.dominantSpeakerManager = dominantSpeakerManager;
const styleManager = new StyleManager();
window.styleManager = styleManager;
const userManager = new UserManager(ws);
window.userManager = userManager;
const participantSpeechStartStopManager = new ParticipantSpeechStartStopManager();
window.participantSpeechStartStopManager = participantSpeechStartStopManager;
const mixedAudioStreamManager = new MixedAudioStreamManager();
window.mixedAudioStreamManager = mixedAudioStreamManager;

const turnOnCameraArialLabel = "start my video"
const turnOffCameraArialLabel = "stop my video"
const turnOnMicArialLabel = "unmute my microphone"
const turnOffMicArialLabel = "mute my microphone"
const turnOnScreenshareArialLabel = "Share Screen"
const turnOffScreenshareClass = "sharer-button--stop"

async function turnOnCamera() {
    // Click camera button to turn it on
    let cameraButton = null;
    const numAttempts = 30;
    for (let i = 0; i < numAttempts; i++) {
        cameraButton = document.querySelector(`button[aria-label="${turnOnCameraArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnCameraArialLabel}"]`);
        if (cameraButton) {
            break;
        }
        window.ws.sendJson({
            type: 'Error',
            message: 'Camera button not found in turnOnCamera, but will try again'
        });
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    
    if (cameraButton) {
        console.log("Clicking the camera button to turn it on");
        cameraButton.click();
    } else {
        console.log("Camera button not found");
        window.ws.sendJson({
            type: 'Error',
            message: 'Camera button not found in turnOnCamera'
        });
    }
}

function turnOnMic() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector(`button[aria-label="${turnOnMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }
}

function turnOffMic() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector(`button[aria-label="${turnOffMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }
}

function turnOnMicAndCamera() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector(`button[aria-label="${turnOnMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }

    // Click camera button to turn it on
    const cameraButton = document.querySelector(`button[aria-label="${turnOnCameraArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnCameraArialLabel}"]`);
    if (cameraButton) {
        console.log("Clicking the camera button to turn it on");
        cameraButton.click();
    } else {
        console.log("Camera button not found");
    }
}

function turnOffMicAndCamera() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector(`button[aria-label="${turnOffMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click camera button to turn it off
    const cameraButton = document.querySelector(`button[aria-label="${turnOffCameraArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffCameraArialLabel}"]`);
    if (cameraButton) {
        console.log("Clicking the camera button to turn it off");
        cameraButton.click();
    } else {
        console.log("Camera off button not found");
    }
}

function turnOnMicAndScreenshare() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector(`button[aria-label="${turnOnMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }

    // Click screenshare button to turn it on
    const screenshareButton = document.querySelector(`button[aria-label="${turnOnScreenshareArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnScreenshareArialLabel}"]`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it on");
        screenshareButton.click();
    } else {
        console.log("Screenshare button not found");
    }
}

function turnOffMicAndScreenshare() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector(`button[aria-label="${turnOffMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click screenshare button to turn it off
    const screenshareButton = document.querySelector(`.${turnOffScreenshareClass}`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it off");
        screenshareButton.click();
    } else {
        console.log("Screenshare off button not found");
    }
}

function turnOnScreenshare() {
    // Click screenshare button to turn it on
    const screenshareButton = document.querySelector(`button[aria-label="${turnOnScreenshareArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnScreenshareArialLabel}"]`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it on");
        screenshareButton.click();
    } else {
        console.log("Screenshare button not found");
    }
}

function turnOffScreenshare() {
    // Click screenshare button to turn it off
    const screenshareButton = document.querySelector(`.${turnOffScreenshareClass}`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it off");
        screenshareButton.click();
    } else {
        console.log("Screenshare off button not found");
    }
}

// BotOutputManager is defined in shared_chromedriver_payload.js

botOutputManager = new BotOutputManager({
    turnOnWebcam: turnOnCamera,
    turnOffWebcam: () => {
        console.log("Turning off webcam");
    },
    turnOnScreenshare: turnOnScreenshare,
    turnOffScreenshare: turnOffScreenshare,
    turnOnMic: turnOnMic,
    turnOffMic: turnOffMic,
    callOriginalGetUserMedia: true,
});

window.botOutputManager = botOutputManager;
