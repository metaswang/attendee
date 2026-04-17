(() => {
    if (globalThis.__realConsole) return;
  
    const c = window.console;
    const bind = Function.prototype.bind;
  
    // Save bound methods so later overwrites don't matter
    globalThis.__realConsole = {
      log:   c?.log   ? bind.call(c.log, c)   : () => {},
      info:  c?.info  ? bind.call(c.info, c)  : () => {},
      warn:  c?.warn  ? bind.call(c.warn, c)  : () => {},
      error: c?.error ? bind.call(c.error, c) : () => {},
      debug: c?.debug ? bind.call(c.debug, c) : () => {},
    };
  })();

const teamsVideoProbeCanvas = document.createElement('canvas');
teamsVideoProbeCanvas.width = 8;
teamsVideoProbeCanvas.height = 8;
const teamsVideoProbeCtx = teamsVideoProbeCanvas.getContext('2d', { willReadFrequently: true });

function summarizeDrawablePixels(drawable, width, height) {
    if (!teamsVideoProbeCtx || !width || !height) {
        return null;
    }

    teamsVideoProbeCtx.clearRect(0, 0, teamsVideoProbeCanvas.width, teamsVideoProbeCanvas.height);
    teamsVideoProbeCtx.drawImage(drawable, 0, 0, width, height, 0, 0, teamsVideoProbeCanvas.width, teamsVideoProbeCanvas.height);
    const imageData = teamsVideoProbeCtx.getImageData(0, 0, teamsVideoProbeCanvas.width, teamsVideoProbeCanvas.height).data;

    let totalLuma = 0;
    let nonBlackPixels = 0;
    let hash = 0;
    for (let i = 0; i < imageData.length; i += 4) {
        const r = imageData[i];
        const g = imageData[i + 1];
        const b = imageData[i + 2];
        const a = imageData[i + 3];
        const luma = Math.round(0.2126 * r + 0.7152 * g + 0.0722 * b);
        totalLuma += luma;
        if (luma > 16 || a > 16) {
            nonBlackPixels += 1;
        }
        hash = ((hash * 131) + r * 3 + g * 5 + b * 7 + a * 11) >>> 0;
    }

    const pixelCount = imageData.length / 4;
    return {
        avg_luma: Math.round(totalLuma / pixelCount),
        non_black_ratio: Number((nonBlackPixels / pixelCount).toFixed(3)),
        hash: hash.toString(16),
    };
}

class StyleManager {
    constructor() {
        this.audioContext = null;
        this.audioTracks = [];
        this.silenceThreshold = 0.0;
        this.silenceCheckInterval = null;
        this.frameStyleElement = null;
        this.frameAdjustInterval = null;
        this.neededInteractionsInterval = null;
        this.fakeUserActivityInterval = null;

        // Stream used which combines the audio tracks from the meeting. Does NOT include the bot's audio
        this.meetingAudioStream = null;
        this.videoTracks = new Map();
    }

    addAudioTrack(audioTrack) {
        this.audioTracks.push(audioTrack);
        if (this.audioTracks.length > 1) {
            window.ws?.sendJson({
                type: 'MultipleAudioTracksDetected',
                numberOfTracks: this.audioTracks.length,
            });
        }
    }

    checkAudioActivity() {
        // Get audio data
        this.analyser.getByteTimeDomainData(this.audioDataArray);
        
        // Calculate deviation from the center value (128)
        let sumDeviation = 0;
        for (let i = 0; i < this.audioDataArray.length; i++) {
            // Calculate how much each sample deviates from the center (128)
            sumDeviation += Math.abs(this.audioDataArray[i] - 128);
        }
        
        const averageDeviation = sumDeviation / this.audioDataArray.length;
        
        // If average deviation is above threshold, we have audio activity
        if (averageDeviation > this.silenceThreshold) {
            window.ws.sendJson({
                type: 'SilenceStatus',
                isSilent: false
            });
        }
    }

    // Prevents Teams from going into mode where it stops receiving chat messages
    fakeUserActivity() {
        const clientX = Math.random() * 500;
        const clientY = Math.random() * 500;
        document.body.dispatchEvent(new MouseEvent("mousemove", {
            bubbles: true,
            clientX: clientX,
            clientY: clientY,
          }));
        window.ws?.sendJson({
            type: 'FakeUserActivity',
            activity: `mousemove: ${clientX}, ${clientY}`
        });
    }

    checkNeededInteractions() {
        // Check if bot has been removed from the meeting
        const removedFromMeetingElement = document.getElementById('calling-retry-screen-title');
        if (removedFromMeetingElement && 
            removedFromMeetingElement.textContent.includes("You've been removed from this meeting")) {
            window.ws.sendJson({
                type: 'MeetingStatusChange',
                change: 'removed_from_meeting'
            });
            console.log('Bot was removed from meeting, sent notification');
        }

        // We need to open the chat window to be able to track messages
        const chatButton = document.querySelector('button#chat-button');
        if (chatButton && !this.chatButtonClicked) {
            chatButton.click();
            this.chatButtonClicked = true;
            
            // Wait until the chat input element appears in the DOM
            this.waitForChatInputAndSendReadyMessage();
        }
    }

    waitForChatInputAndSendReadyMessage() {
        const checkForChatInput = () => {
            const chatInput = document.querySelector('[aria-label="Type a message"], [placeholder="Type a message"]');
            if (chatInput) {
                // Chat input is now available, send the ready message
                window.ws.sendJson({
                    type: 'ChatStatusChange',
                    change: 'ready_to_send'
                });
                console.log('Chat input element found, ready to send messages');
            } else {
                // Chat input not found yet, check again in 500ms
                setTimeout(checkForChatInput, 500);
            }
        };
        
        // Start checking for the chat input element
        checkForChatInput();
    }

    startSilenceDetection() {
         // Set up audio context and processing as before
         this.audioContext = new AudioContext();

         this.audioSources = this.audioTracks.map(track => {
             const mediaStream = new MediaStream([track]);
             return this.audioContext.createMediaStreamSource(mediaStream);
         });
 
         // Create a destination node
         const destination = this.audioContext.createMediaStreamDestination();
 
         // Connect all sources to the destination
         this.audioSources.forEach(source => {
             source.connect(destination);
         });
 
         // Create analyzer and connect it to the destination
         this.analyser = this.audioContext.createAnalyser();
         this.analyser.fftSize = 256;
         const bufferLength = this.analyser.frequencyBinCount;
         this.audioDataArray = new Uint8Array(bufferLength);
 
         // Create a source from the destination's stream and connect it to the analyzer
         const mixedSource = this.audioContext.createMediaStreamSource(destination.stream);
         mixedSource.connect(this.analyser);
 
         this.mixedAudioTrack = destination.stream.getAudioTracks()[0];

        // Process and send mixed audio if enabled
        if (window.initialData.sendMixedAudio && this.mixedAudioTrack) {
            this.processMixedAudioTrack();
        }

        // Clear any existing interval
        if (this.silenceCheckInterval) {
            clearInterval(this.silenceCheckInterval);
        }
                
        if (this.neededInteractionsInterval) {
            clearInterval(this.neededInteractionsInterval);
        }
                
        if (this.fakeUserActivityInterval) {
            clearInterval(this.fakeUserActivityInterval);
        }

        // Check for audio activity every second
        this.silenceCheckInterval = setInterval(() => {
            this.checkAudioActivity();
        }, 1000);

        // Check for needed interactions every 5 seconds
        this.neededInteractionsInterval = setInterval(() => {
            this.checkNeededInteractions();
        }, 5000);

        // Perform fake user activity every 4 minutes
        this.fakeUserActivityInterval = setInterval(() => {
            this.fakeUserActivity();
        }, 240000);

        this.meetingAudioStream = destination.stream;
        window.ws?.emitDiagnosticEvent?.('MeetingAudioStreamReady', {
            audio_track_count: destination.stream.getAudioTracks().length,
            source_track_count: this.audioTracks.length,
            audio_tracks: destination.stream.getAudioTracks().map((track) => ({
                id: track.id,
                kind: track.kind,
                enabled: track.enabled,
                muted: track.muted,
                ready_state: track.readyState,
                label: track.label || null,
            })),
        });
    }
    
    getMeetingAudioStream() {
        return this.meetingAudioStream;
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
    }

    getVideoTrackEntryForRecording() {
        const desiredStreamId = virtualStreamToPhysicalStreamMappingManager?.getVideoStreamIdToSend?.();
        const liveTracks = Array.from(this.videoTracks.values()).filter((item) => item.track?.readyState === 'live');
        if (!liveTracks.length) {
            return null;
        }
        if (desiredStreamId) {
            const desiredTrack = liveTracks.find((item) => item.streamId === desiredStreamId);
            if (desiredTrack) {
                return desiredTrack;
            }
        }
        return liveTracks.sort((a, b) => b.firstSeenAt - a.firstSeenAt)[0] || null;
    }

    getVideoTrackForRecording() {
        return this.getVideoTrackEntryForRecording()?.track || null;
    }

    getRenderableElementForRecording() {
        const centralElement = document.querySelector('[data-test-segment-type="central"]');
        const scopes = centralElement ? [centralElement, document.body] : [document.body];
        const candidates = [];

        const isVisible = (element) => {
            if (!element?.isConnected) {
                return false;
            }
            const style = window.getComputedStyle(element);
            if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
                return false;
            }
            const rect = element.getBoundingClientRect();
            return rect.width >= 32 && rect.height >= 32;
        };

        for (const scope of scopes) {
            for (const element of scope.querySelectorAll('video, canvas')) {
                if (element === window.ws?.videoChunkCanvas || element === window.ws?.videoChunkSourceCanvas) {
                    continue;
                }
                if (!isVisible(element)) {
                    continue;
                }
                if (element.tagName === 'VIDEO' && !(element.videoWidth > 0 && element.videoHeight > 0)) {
                    continue;
                }
                if (element.tagName === 'CANVAS' && !(element.width > 0 && element.height > 0)) {
                    continue;
                }
                const rect = element.getBoundingClientRect();
                candidates.push({
                    element,
                    area: rect.width * rect.height,
                    inCentralPane: !!centralElement && centralElement.contains(element),
                });
            }
            if (candidates.length) {
                break;
            }
        }

        if (!candidates.length) {
            return null;
        }

        candidates.sort((a, b) => {
            if (a.inCentralPane !== b.inCentralPane) {
                return a.inCentralPane ? -1 : 1;
            }
            return b.area - a.area;
        });

        return candidates[0].element;
    }

    async processMixedAudioTrack() {
        try {
            // Create processor to get raw audio frames from the mixed audio track
            const processor = new MediaStreamTrackProcessor({ track: this.mixedAudioTrack });
            const generator = new MediaStreamTrackGenerator({ kind: 'audio' });
            
            // Get readable stream of audio frames
            const readable = processor.readable;
            const writable = generator.writable;

            // Transform stream to intercept and send audio frames
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
                        // If multi-channel, average all channels together to create mono output
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

                        // Send mixed audio data via websocket
                        const timestamp = performance.now();
                        window.ws.sendMixedAudio(timestamp, audioData);
                        
                        // Pass through the original frame
                        controller.enqueue(frame);
                    } catch (error) {
                        console.error('Error processing mixed audio frame:', error);
                        frame.close();
                    }
                },
                flush() {
                    console.log('Mixed audio transform stream flush called');
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
                            console.error('Mixed audio pipeline error:', error);
                        }
                    });
            } catch (error) {
                console.error('Mixed audio stream pipeline error:', error);
                abortController.abort();
            }

        } catch (error) {
            console.error('Error setting up mixed audio processor:', error);
        }
    }
 
    makeMainVideoFillFrame = function() {
        /* ── 0.  Cleanup from earlier runs ─────────────────────────────── */
        if (this.blanket?.isConnected) this.blanket.remove();
        if (this.frameStyleElement?.isConnected) this.frameStyleElement.remove();
        if (this.frameAdjustInterval) {
            cancelAnimationFrame(this.frameAdjustInterval);   // ← was clearInterval
            this.frameAdjustInterval = null;
        }
    
        /* ── 1.  Inject the blanket ────────────────────────────────────── */
        const blanket = document.createElement("div");
        blanket.id = "attendee-blanket";
        Object.assign(blanket.style, {
            position: "fixed",
            inset: "0",
            background: "#fff",
            zIndex: 1998,              // below the video we’ll promote
            pointerEvents: "none"      // lets events fall through
        });
        document.body.appendChild(blanket);
        this.blanket = blanket;
    
        /* ── 2.  Promote the central video and its descendants ─────────── */
        const style = document.createElement("style");
        style.textContent = `
            /* central pane fills the viewport, highest z‑index */
            [data-test-segment-type="central"] {
                position: fixed !important;
                inset: 0 !important;
                width: 100vw !important;
                height: 100vh !important;
                z-index: 1999 !important;   /* > blanket */
            }
            /* make sure its children inherit size & events normally */
            [data-test-segment-type="central"], 
            [data-test-segment-type="central"] * {
                pointer-events: auto !important;
            }
        `;
        document.head.appendChild(style);
        this.frameStyleElement = style;
    
        /* ── 3.  Keep the central element the right size ───────────────── */
        const adjust = () => {
            this.adjustCentralElement?.();
            this.frameAdjustInterval = requestAnimationFrame(adjust);  // ← RAF loop
        };
        adjust();  // kick it off
    }
    
    adjustCentralElement = function() {
        // Get the central element
        const centralElement = document.querySelector('[data-test-segment-type="central"]');
        
        // Function to resize the central element
        function adjustCentralElementSize(element) {
            if (element?.style) {
                element.style.width  = `${window.initialData.videoFrameWidth}px`;
                element.style.height = `${window.initialData.videoFrameHeight}px`;
                element.style.position = 'fixed';
            }
        }
    
        function adjustChildElement(element) {
            if (element?.style) {
                element.style.position = 'fixed';
                element.style.width  = '100%';
                element.style.height = '100%';
                element.style.top  = '0';
                element.style.left = '0';
            }
        }
        
        if (centralElement) {
            adjustChildElement(centralElement?.children[0]?.children[0]?.children[0]?.children[0]?.children[0]);
            adjustChildElement(centralElement?.children[0]?.children[0]?.children[0]?.children[0]);
            adjustChildElement(centralElement?.children[0]?.children[0]?.children[0]);
            adjustChildElement(centralElement?.children[0]);
            adjustCentralElementSize(centralElement);
        }
    }
    
    restoreOriginalFrame = function() {
        // If we have a reference to the style element, remove it
        if (this.frameStyleElement) {
            this.frameStyleElement.remove();
            this.frameStyleElement = null;
            console.log('Removed video frame style element');
        }
        
        // Cancel the RAF loop if it exists
        if (this.frameAdjustInterval) {
            cancelAnimationFrame(this.frameAdjustInterval);   // ← was clearInterval
            this.frameAdjustInterval = null;
        }
    }

    stop() {
        // Clear any existing interval
        if (this.silenceCheckInterval) {
            clearInterval(this.silenceCheckInterval);
            this.silenceCheckInterval = null;
        }
        
        if (this.neededInteractionsInterval) {
            clearInterval(this.neededInteractionsInterval);
            this.neededInteractionsInterval = null;
        }
        
        if (this.fakeUserActivityInterval) {
            clearInterval(this.fakeUserActivityInterval);
            this.fakeUserActivityInterval = null;
        }
        
        // Restore original frame layout
        this.restoreOriginalFrame();
        
        console.log('Stopped StyleManager');
    }

    start() {
        this.startSilenceDetection();

        if (window.teamsInitialData.modifyDomForVideoRecording) {
            this.makeMainVideoFillFrame();
        }

        console.log('Started StyleManager');
    }
    
    addVideoTrack(trackEvent) {
        window.styleManager?.addVideoTrack?.(trackEvent);
    }
}

class DominantSpeakerManager {
    constructor() {
        this.dominantSpeakerStreamId = null;
        this.captionAudioTimes = [];
        this.speechIntervalsPerParticipant = {};
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

    getSpeakerIdForTimestampMsUsingSpeechIntervals(timestampMs) {
        const speakersAtTimestamp = [];
        
        // Check each participant to see if they have a speech interval at the given timestamp
        for (const [speakerId, intervals] of Object.entries(this.speechIntervalsPerParticipant)) {
            
            let isCurrentlySpeaking = false;
            let timestampMsOfLastStart = null;
            
            // Process each interval event to determine if participant is speaking at timestampMs
            for (const interval of intervals) {
                if (interval.timestampMs > timestampMs) {
                    // We've passed the timestamp, stop checking this participant
                    break;
                }
                
                if (interval.type === 'start') {
                    isCurrentlySpeaking = true;
                    timestampMsOfLastStart = interval.timestampMs;
                } else if (interval.type === 'end') {
                    isCurrentlySpeaking = false;
                }
            }
            
            if (isCurrentlySpeaking) {
                speakersAtTimestamp.push({
                    speakerId,
                    timestampMsOfLastStart
                });
            }
        }
        
        if (speakersAtTimestamp.length === 0)
            return null;

        if (speakersAtTimestamp.length === 1)
            return speakersAtTimestamp[0].speakerId;

        // If there were multiple speakers in this interval, we need a "tie breaker"

        // If we have captions, then look at the participant for the last caption audio time
        if (this.captionAudioTimes.length > 0)
        {
            const participantForLastCaptionAudioTime = this.getLastSpeakerIdForTimestampMs(timestampMs);
            if (participantForLastCaptionAudioTime && speakersAtTimestamp.some(speaker => speaker.speakerId === participantForLastCaptionAudioTime))
                return participantForLastCaptionAudioTime;
        }

        // Otherwise use the the speaker with the earliest timestampMsOfLastStart
        return speakersAtTimestamp.reduce((min, speaker) => speaker.timestampMsOfLastStart < min.timestampMsOfLastStart ? speaker : min).speakerId;

        // Otherwise use the speaker with the highest timestampMsOfLastStart (Not using)
        // return speakersAtTimestamp.reduce((max, speaker) => speaker.timestampMsOfLastStart > max.timestampMsOfLastStart ? speaker : max).speakerId;
    }

    addSpeechIntervalStart(timestampMs, speakerId) {
        if (!this.speechIntervalsPerParticipant[speakerId])
            this.speechIntervalsPerParticipant[speakerId] = [];

        this.speechIntervalsPerParticipant[speakerId].push({type: 'start', timestampMs: timestampMs});

        // Not going to send this to server for now.
        /*
        window.ws.sendJson({
            type: 'SpeechStart',
            participant_uuid: speakerId,
            timestamp: timestampMs
        });
        */
    }

    addSpeechIntervalEnd(timestampMs, speakerId) {
        if (!this.speechIntervalsPerParticipant[speakerId])
            this.speechIntervalsPerParticipant[speakerId] = [];

        this.speechIntervalsPerParticipant[speakerId].push({type: 'end', timestampMs: timestampMs});

        // Not going to send this to server for now.
        /*
        window.ws.sendJson({
            type: 'SpeechStop',
            participant_uuid: speakerId,
            timestamp: timestampMs
        });
        */
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

// Virtual to Physical Stream Mapping Manager
// Microsoft Teams has virtual streams which are referenced by a sourceId
// An instance of the teams client has a finite number of phyisical streams which are referenced by a streamId
// This class manages the mapping between virtual and physical streams
class VirtualStreamToPhysicalStreamMappingManager {
    constructor() {
        this.virtualStreams = new Map();
        this.physicalStreamsByClientStreamId = new Map();
        this.physicalStreamsByServerStreamId = new Map();

        this.physicalClientStreamIdToVirtualStreamIdMapping = {}
        this.virtualStreamIdToPhysicalClientStreamIdMapping = {}
        this.lastLoggedVideoSelectionSnapshot = null;
    }

    getCurrentUserId() {
        return window.callManager?.getCurrentUserId?.();
    }

    logVideoSelectionDecision(payload) {
        const snapshot = JSON.stringify(payload);
        if (snapshot === this.lastLoggedVideoSelectionSnapshot) {
            return;
        }
        this.lastLoggedVideoSelectionSnapshot = snapshot;
        realConsole?.log('video stream selection decision', snapshot);
        window.ws?.sendJson({
            type: 'VideoSelectionDecision',
            decision: payload,
        });
    }

    getVirtualVideoStreamIdToSend() {
        // If there is an active screenshare stream return that stream's virtual id

        // If there is an active dominant speaker video stream return that stream id

        // Otherwise return the first virtual stream id that has an associated physical stream
        //realConsole?.log('Object.values(this.virtualStreams)', Object.values(this.virtualStreams));
        //realConsole?.log("STARTFILTER");
        const virtualSteamsThatHavePhysicalStreams = []
        const currentUserId = this.getCurrentUserId();
        for (const virtualStream of this.virtualStreams.values()) {
            const hasCorrespondingPhysicalStream = this.virtualStreamIdToPhysicalClientStreamIdMapping[virtualStream.sourceId];
            const isCurrentUserStream = !!currentUserId && virtualStream.participant?.id === currentUserId;

            //realConsole?.log('zzzphysicalClientStreamIds', physicalClientStreamIds);
            //realConsole?.log('zzzvirtualStream.sourceId.toString()', virtualStream.sourceId.toString());
            //realConsole?.log('zzzthis.physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping);
            //realConsole?.log('zzzvirtualStream', virtualStream);
            //const cond1 = (virtualStream.type === 'video' || virtualStream.type === 'applicationsharing-video');
            //const cond2 = !physicalClientStreamIds.includes(virtualStream.sourceId.toString());
            //const cond3 = hasCorrespondingPhysicalStream;
            //realConsole?.log('zzzcond1', cond1, 'cond2', cond2, 'cond3', cond3);


            if ((virtualStream.isScreenShare || virtualStream.isWebcam) && !isCurrentUserStream && hasCorrespondingPhysicalStream)
            {
                virtualSteamsThatHavePhysicalStreams.push(virtualStream);
            }
        };
        //realConsole?.log("ENDFILTER");
        //realConsole?.log('zzzvirtualSteamsThatHavePhysicalStreams', virtualSteamsThatHavePhysicalStreams);
        //realConsole?.log('this.physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping);
        if (virtualSteamsThatHavePhysicalStreams.length == 0) {
            this.logVideoSelectionDecision({
                stage: 'virtual',
                reason: 'no_virtual_stream_with_physical_mapping',
                current_user_id: currentUserId || null,
                total_virtual_streams: this.virtualStreams.size,
                mapped_virtual_stream_count: Object.keys(this.virtualStreamIdToPhysicalClientStreamIdMapping).length,
            });
            return null;
        }

        const firstActiveScreenShareStream = virtualSteamsThatHavePhysicalStreams.find(virtualStream => virtualStream.isScreenShare && virtualStream.isActive);
        //realConsole?.log('zzzfirstActiveScreenShareStream', firstActiveScreenShareStream);
        if (firstActiveScreenShareStream) {
            this.logVideoSelectionDecision({
                stage: 'virtual',
                reason: 'active_screenshare',
                selected_virtual_stream_id: firstActiveScreenShareStream.sourceId,
                participant_id: firstActiveScreenShareStream.participant?.id || null,
                current_user_id: currentUserId || null,
            });
            return firstActiveScreenShareStream.sourceId;
        }

        const dominantSpeaker = dominantSpeakerManager.getDominantSpeaker();
        //realConsole?.log('zzzdominantSpeaker', dominantSpeaker);
        if (dominantSpeaker)
        {
            const dominantSpeakerVideoStream = virtualSteamsThatHavePhysicalStreams.find(virtualStream => virtualStream.participant.id === dominantSpeaker.id && virtualStream.isWebcam && virtualStream.isActive);
            if (dominantSpeakerVideoStream) {
                this.logVideoSelectionDecision({
                    stage: 'virtual',
                    reason: 'dominant_speaker_webcam',
                    selected_virtual_stream_id: dominantSpeakerVideoStream.sourceId,
                    participant_id: dominantSpeakerVideoStream.participant?.id || null,
                    dominant_speaker_id: dominantSpeaker.id,
                    current_user_id: currentUserId || null,
                });
                return dominantSpeakerVideoStream.sourceId;
            }
        }

        const fallbackVirtualStream = virtualSteamsThatHavePhysicalStreams[0] || null;
        this.logVideoSelectionDecision({
            stage: 'virtual',
            reason: 'first_mapped_video_stream',
            selected_virtual_stream_id: fallbackVirtualStream?.sourceId || null,
            participant_id: fallbackVirtualStream?.participant?.id || null,
            current_user_id: currentUserId || null,
        });
        return fallbackVirtualStream?.sourceId;
    }

    getVideoStreamIdToSend() {
        
        const virtualVideoStreamIdToSend = this.getVirtualVideoStreamIdToSend();
        if (!virtualVideoStreamIdToSend) {
            this.logVideoSelectionDecision({
                stage: 'physical',
                reason: 'missing_virtual_video_stream_id',
                selected_server_stream_id: null,
            });
            return null;
        }
        //realConsole?.log('virtualVideoStreamIdToSend', virtualVideoStreamIdToSend);
        //realConsole?.log('this.physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping);

        //realConsole?.log('Object.entries(this.physicalClientStreamIdToVirtualStreamIdMapping)', Object.entries(this.physicalClientStreamIdToVirtualStreamIdMapping));

        // Find the physical client stream ID that maps to this virtual stream ID
        const physicalClientStreamId = this.virtualStreamIdToPhysicalClientStreamIdMapping[virtualVideoStreamIdToSend];
            
        //realConsole?.log('physicalClientStreamId', physicalClientStreamId);
        //realConsole?.log('this.physicalStreamsByClientStreamId', this.physicalStreamsByClientStreamId);
        if (!physicalClientStreamId) {
            this.logVideoSelectionDecision({
                stage: 'physical',
                reason: 'missing_physical_client_stream_id',
                selected_virtual_stream_id: virtualVideoStreamIdToSend,
                selected_server_stream_id: null,
            });
            return null;
        }

        const physicalStream = this.physicalStreamsByClientStreamId.get(physicalClientStreamId);
        if (!physicalStream) {
            this.logVideoSelectionDecision({
                stage: 'physical',
                reason: 'missing_physical_stream_entry',
                selected_virtual_stream_id: virtualVideoStreamIdToSend,
                selected_physical_client_stream_id: physicalClientStreamId,
                selected_server_stream_id: null,
            });
            return null;
        }

        //realConsole?.log('physicalStream', physicalStream);
        this.logVideoSelectionDecision({
            stage: 'physical',
            reason: 'resolved_server_stream_id',
            selected_virtual_stream_id: virtualVideoStreamIdToSend,
            selected_physical_client_stream_id: physicalClientStreamId,
            selected_server_stream_id: physicalStream.serverStreamId,
        });
            
        return physicalStream.serverStreamId;
    }

    upsertPhysicalStreams(physicalStreams) {
        for (const physicalStream of physicalStreams) {
            this.physicalStreamsByClientStreamId.set(physicalStream.clientStreamId, physicalStream);
            this.physicalStreamsByServerStreamId.set(physicalStream.serverStreamId, physicalStream);
        }
        realConsole?.log('physicalStreamsByClientStreamId', this.physicalStreamsByClientStreamId);
        realConsole?.log('physicalStreamsByServerStreamId', this.physicalStreamsByServerStreamId);
    }

    upsertVirtualStream(virtualStream) {
        realConsole?.log('upsertVirtualStream', virtualStream, 'this.virtualStreams', this.virtualStreams);
        this.virtualStreams.set(virtualStream.sourceId.toString(), {...virtualStream, sourceId: virtualStream.sourceId.toString()});
    }
    
    removeVirtualStreamsForParticipant(participantId) {
        const virtualStreamsToRemove = Array.from(this.virtualStreams.values()).filter(virtualStream => virtualStream.participant.id === participantId);
        for (const virtualStream of virtualStreamsToRemove) {
            this.virtualStreams.delete(virtualStream.sourceId.toString());
        }
    }

    upsertPhysicalClientStreamIdToVirtualStreamIdMapping(physicalClientStreamId, virtualStreamId) {
        const physicalClientStreamIdString = physicalClientStreamId.toString();
        const virtualStreamIdString = virtualStreamId.toString();
        if (virtualStreamIdString === '-1')
        {
            // Find and delete from the inverse mapping first
            const virtualStreamIdToDelete = this.physicalClientStreamIdToVirtualStreamIdMapping[physicalClientStreamIdString];
            if (virtualStreamIdToDelete) {
                delete this.virtualStreamIdToPhysicalClientStreamIdMapping[virtualStreamIdToDelete];
            }
            else {
                realConsole?.error('Entry for virtual stream id ', virtualStreamIdToDelete, ' not found in', this.virtualStreamIdToPhysicalClientStreamIdMapping);
            }
            // Then delete from the main mapping
            delete this.physicalClientStreamIdToVirtualStreamIdMapping[physicalClientStreamIdString];
        }
        else {
            this.physicalClientStreamIdToVirtualStreamIdMapping[physicalClientStreamIdString] = virtualStreamIdString;
            this.virtualStreamIdToPhysicalClientStreamIdMapping[virtualStreamIdString] = physicalClientStreamIdString;
        }
        realConsole?.log('physicalClientStreamId', physicalClientStreamIdString, 'virtualStreamId', virtualStreamIdString, 'physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping, 'virtualStreamIdToPhysicalClientStreamIdMapping', this.virtualStreamIdToPhysicalClientStreamIdMapping);
    }

    virtualStreamIdToParticipant(virtualStreamId) {
        return this.virtualStreams.get(virtualStreamId)?.participant;
    }

    physicalServerStreamIdToParticipant(physicalServerStreamId) {
        realConsole?.log('physicalServerStreamId', physicalServerStreamId);
        realConsole?.log('physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping);

        const physicalClientStreamId = this.physicalStreamsByServerStreamId.get(physicalServerStreamId)?.clientStreamId;
        realConsole?.log('physicalClientStreamId', physicalClientStreamId);
        if (!physicalClientStreamId)
            return null;

        const virtualStreamId = this.physicalClientStreamIdToVirtualStreamIdMapping[physicalClientStreamId];
        if (!virtualStreamId)
            return null;

        const participant = this.virtualStreams.get(virtualStreamId)?.participant;
        if (!participant)
            return null;

        return participant;
    }
}



class RTCInterceptor {
    constructor(callbacks) {
        // Store the original RTCPeerConnection
        const originalRTCPeerConnection = window.RTCPeerConnection;
        
        // Store callbacks
        const onPeerConnectionCreate = callbacks.onPeerConnectionCreate || (() => {});
        const onDataChannelCreate = callbacks.onDataChannelCreate || (() => {});
        const onDataChannelSend = callbacks.onDataChannelSend || (() => {});
        
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
                
                // Intercept send method
                const originalSend = dataChannel.send;
                dataChannel.send = function(data) {
                    try {
                        onDataChannelSend({
                            channel: dataChannel,
                            data: data,
                            peerConnection: peerConnection
                        });
                    } catch (error) {
                        realConsole?.error('Error in data channel send interceptor:', error);
                    }
                    return originalSend.apply(this, arguments);
                };
                
                onDataChannelCreate(dataChannel, peerConnection);
                return dataChannel;
            };
            
            // Intercept createOffer
            const originalCreateOffer = peerConnection.createOffer.bind(peerConnection);
            peerConnection.createOffer = async function(options) {
                const offer = await originalCreateOffer(options);
                realConsole?.log('from peerConnection.createOffer:', offer.sdp);
                /*
                console.log('Created Offer SDP:', {
                    type: offer.type,
                    sdp: offer.sdp,
                    parsedSDP: parseSDP(offer.sdp)
                });
                */
                realConsole?.log('from peerConnection.createOffer: extractStreamIdToSSRCMappingFromSDP = ', extractStreamIdToSSRCMappingFromSDP(offer.sdp));
                return offer;
            };

            // Intercept createAnswer
            const originalCreateAnswer = peerConnection.createAnswer.bind(peerConnection);
            peerConnection.createAnswer = async function(options) {
                const answer = await originalCreateAnswer(options);
                realConsole?.log('from peerConnection.createAnswer:', answer.sdp);
                /*
                console.log('Created Answer SDP:', {
                    type: answer.type,
                    sdp: answer.sdp,
                    parsedSDP: parseSDP(answer.sdp)
                });
                */
                realConsole?.log('from peerConnection.createAnswer: extractStreamIdToSSRCMappingFromSDP = ', extractStreamIdToSSRCMappingFromSDP(answer.sdp));
                return answer;
            };
       
            

/*

how the mapping works:
the SDP contains x-source-streamid:<some value>
this corresponds to the stream id / source id in the participants hash
So that correspondences allows us to map a participant stream to an SDP. But how do we go from SDP to the raw low level track id? 
The tracks have a streamId that looks like this mainVideo-39016. The SDP has that same streamId contained within it in the msid: header
3396





*/
            // Override setLocalDescription with detailed logging
            const originalSetLocalDescription = peerConnection.setLocalDescription;
            peerConnection.setLocalDescription = async function(description) {
                realConsole?.log('from peerConnection.setLocalDescription:', description.sdp);
                /*
                console.log('Setting Local SDP:', {
                    type: description.type,
                    sdp: description.sdp,
                    parsedSDP: parseSDP(description.sdp)
                });
                */
                realConsole?.log('from peerConnection.setLocalDescription: extractStreamIdToSSRCMappingFromSDP = ', extractStreamIdToSSRCMappingFromSDP(description.sdp));
                return originalSetLocalDescription.apply(this, arguments);
            };

            // Override setRemoteDescription with detailed logging
            const originalSetRemoteDescription = peerConnection.setRemoteDescription;
            peerConnection.setRemoteDescription = async function(description) {
                realConsole?.log('from peerConnection.setRemoteDescription:', description.sdp);
                /*
                console.log('Setting Remote SDP:', {
                    type: description.type,
                    parsedSDP: parseSDP(description.sdp)
                });
                */
                const mapping = extractStreamIdToSSRCMappingFromSDP(description.sdp);
                realConsole?.log('from peerConnection.setRemoteDescription: extractStreamIdToSSRCMappingFromSDP = ', mapping);
                virtualStreamToPhysicalStreamMappingManager.upsertPhysicalStreams(mapping);
                return originalSetRemoteDescription.apply(this, arguments);
            };

            function extractMSID(rawSSRCEntry) {
                if (!rawSSRCEntry) return null;
                
                const parts = rawSSRCEntry.split(' ');
                for (const part of parts) {
                    if (part.startsWith('msid:')) {
                        return part.substring(5).split(' ')[0];
                    }
                }
                return null;
            }

            function extractStreamIdToSSRCMappingFromSDP(sdp)
            {
                const parsedSDP = parseSDP(sdp);
                const mapping = [];
                const sdpMediaList = parsedSDP.media || [];

                for (const sdpMediaEntry of sdpMediaList) {
                    const sdpMediaEntryAttributes = sdpMediaEntry.attributes || {};
                    //realConsole?.log('sdpMediaEntryAttributes', sdpMediaEntryAttributes);
                    //realConsole?.log(sdpMediaEntry);
                    const sdpMediaEntrySSRCNumbersRaw = sdpMediaEntryAttributes.ssrc || [];
                    const sdpMediaEntrySSRCNumbers = [...new Set(sdpMediaEntrySSRCNumbersRaw.map(x => extractMSID(x)))];

                    const streamIds = sdpMediaEntryAttributes['x-source-streamid'] || [];
                    if (streamIds.length > 1)
                        console.warn('Warning: x-source-streamid has multiple stream ids');
                    
                    const streamId = streamIds[0];

                    for(const ssrc of sdpMediaEntrySSRCNumbers)
                        if (ssrc && streamId)
                            mapping.push({clientStreamId: streamId, serverStreamId: ssrc});
                }

                return mapping;
            }

            // Helper function to parse SDP into a more readable format
            function parseSDP(sdp) {
                const parsed = {
                    media: [],
                    attributes: {},
                    version: null,
                    origin: null,
                    session: null,
                    connection: null,
                    timing: null,
                    bandwidth: null
                };
            
                const lines = sdp.split('\r\n');
                let currentMedia = null;
            
                for (const line of lines) {
                    // Handle session-level fields
                    if (line.startsWith('v=')) {
                        parsed.version = line.substr(2);
                    } else if (line.startsWith('o=')) {
                        parsed.origin = line.substr(2);
                    } else if (line.startsWith('s=')) {
                        parsed.session = line.substr(2);
                    } else if (line.startsWith('c=')) {
                        parsed.connection = line.substr(2);
                    } else if (line.startsWith('t=')) {
                        parsed.timing = line.substr(2);
                    } else if (line.startsWith('b=')) {
                        parsed.bandwidth = line.substr(2);
                    } else if (line.startsWith('m=')) {
                        // Media section
                        currentMedia = {
                            type: line.split(' ')[0].substr(2),
                            description: line,
                            attributes: {},
                            connection: null,
                            bandwidth: null
                        };
                        parsed.media.push(currentMedia);
                    } else if (line.startsWith('a=')) {
                        // Handle attributes that may contain multiple colons
                        const colonIndex = line.indexOf(':');
                        let key, value;
                        
                        if (colonIndex === -1) {
                            // Handle flag attributes with no value
                            key = line.substr(2);
                            value = true;
                        } else {
                            key = line.substring(2, colonIndex);
                            value = line.substring(colonIndex + 1);
                        }
            
                        if (currentMedia) {
                            if (!currentMedia.attributes[key]) {
                                currentMedia.attributes[key] = [];
                            }
                            currentMedia.attributes[key].push(value);
                        } else {
                            if (!parsed.attributes[key]) {
                                parsed.attributes[key] = [];
                            }
                            parsed.attributes[key].push(value);
                        }
                    } else if (line.startsWith('c=') && currentMedia) {
                        currentMedia.connection = line.substr(2);
                    } else if (line.startsWith('b=') && currentMedia) {
                        currentMedia.bandwidth = line.substr(2);
                    }
                }
            
                return parsed;
            }

            return peerConnection;
        };
    }
}


class ChatMessageManager {
    constructor(ws) {
        this.ws = ws;
        this.chatMessages = {};
    }

    // The more sophisticated approach gets blocked by trusted html csp
    stripHtml(html) {
        return html.replace(/<[^>]*>/g, '');
    }

    // Teams client sometimes sends duplicate updates, this filters them out.
    isNewOrUpdatedChatMessage(chatMessage) {
        const currentMessage = this.chatMessages[chatMessage.clientMessageId];
        if (!currentMessage)
            return true;
        return currentMessage.content !== chatMessage.content || currentMessage.originalArrivalTime !== chatMessage.originalArrivalTime || currentMessage.from !== chatMessage.from;
    }

    handleChatMessage(chatMessage) {
        try {
            if (!chatMessage.clientMessageId)
                return;
            if (!chatMessage.from)
                return;
            if (!chatMessage.content)
                return;
            if (!chatMessage.originalArrivalTime)
                return;
            // messageTypes we care about are: RichText, RichText/Html, Text, RichText/Sms
            const allowedMessageTypes = ['RichText', 'RichText/Html', 'Text', 'RichText/Sms'];
            if (!allowedMessageTypes.includes(chatMessage.messageType))
            {
                window.ws.sendJson({
                    type: 'chatMessageHadWrongMessageTypeError',
                    chatMessage: chatMessage,
                });
                return;
            }
            if (!this.isNewOrUpdatedChatMessage(chatMessage))
                return;

            this.chatMessages[chatMessage.clientMessageId] = chatMessage;

            const timestamp_ms = new Date(chatMessage.originalArrivalTime).getTime();
            this.ws.sendJson({
                type: 'ChatMessage',
                message_uuid: chatMessage.clientMessageId,
                participant_uuid: chatMessage.from,
                timestamp: Math.floor(timestamp_ms / 1000),
                text: this.stripHtml(chatMessage.content),
            });
        }
        catch (error) {
            console.error('Error in handleChatMessage', error);
            this.ws?.sendJson({
                type: 'ErrorInHandleChatMessage',
                message: error.message
            });
        }
    }
}

// User manager
class UserManager {
    constructor(ws) {
        this.allUsersMap = new Map();
        this.currentUsersMap = new Map();
        this.deviceOutputMap = new Map();

        this.ws = ws;
    }


    getDeviceOutput(deviceId, outputType) {
        return this.deviceOutputMap.get(`${deviceId}-${outputType}`);
    }

    updateDeviceOutputs(deviceOutputs) {
        for (const output of deviceOutputs) {
            const key = `${output.deviceId}-${output.deviceOutputType}`; // Unique key combining device ID and output type

            const deviceOutput = {
                deviceId: output.deviceId,
                outputType: output.deviceOutputType, // 1 = audio, 2 = video
                streamId: output.streamId,
                disabled: output.deviceOutputStatus.disabled,
                lastUpdated: Date.now()
            };

            this.deviceOutputMap.set(key, deviceOutput);
        }

        // Notify websocket clients about the device output update
        this.ws.sendJson({
            type: 'DeviceOutputsUpdate',
            deviceOutputs: Array.from(this.deviceOutputMap.values())
        });
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

    convertUser(user) {
        const currentUserId = window.callManager?.getCurrentUserId();
        return {
            deviceId: user.details.id,
            displayName: user.details.displayName,
            fullName: user.details.displayName,
            profile: '',
            status: user.state,
            humanized_status: user.state === "active" ? "in_meeting" : "not_in_meeting",
            isCurrentUser: (!!currentUserId) && (user.details.id === currentUserId),
            isHost: user.meetingRole === "organizer",
            meetingId: user.callId,
        }
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
                isHost: user.isHost,
                meetingId: user.meetingId
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
                isHost: user.isHost,
                meetingId: user.meetingId
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
var realConsole = globalThis.__realConsole;
// Websocket client
class WebSocketClient {
    // Message types
    static MESSAGE_TYPES = {
        JSON: 1,
        VIDEO: 2,  // Reserved for future use
        AUDIO: 3,   // Reserved for future use
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
        this.videoChunkSourceCanvas = null;
        this.videoChunkSourceCanvasCtx = null;
        this.videoChunkAnimationFrame = null;
        this.videoChunkFrameReader = null;
        this.videoChunkSourceGeneration = 0;
        this.videoChunkSourceTrackId = null;
        this.videoChunkSourceStreamId = null;
        this.videoChunkSourceTrackClone = null;
        this.videoChunkSourceTrackSettings = null;
        this.videoChunkDomSourceElement = null;
        this.videoChunkDomSourceKey = null;
        this.videoChunkLastHealthyFrameAt = null;
        this.videoChunkFirstFrameLoggedAt = null;
        this.videoChunkLastFrameDimensionsKey = null;
        this.videoChunkStallLogState = null;
        this.videoChunkLastLoggedTrackInfo = null;
        this.videoChunkMissingSourceReason = null;
        this.videoProbeInterval = null;
        this.videoTrackProbePromises = new Map();
        this.encodedAudioChunkCount = 0;
        this.encodedVideoChunkCount = 0;
        this.audioChunkRequestCount = 0;
        this.audioChunkZeroSizeCount = 0;
        /*
        We no longer need this because we're not using MediaStreamTrackProcessor's
        this.lastVideoFrameTime = performance.now();
        this.blackFrameInterval = null;
        */
    }
  
    /*
    startBlackFrameTimer() {
      if (this.blackFrameInterval) return; // Don't start if already running
      
      this.blackFrameInterval = setInterval(() => {
          try {
              const currentTime = performance.now();
              if (currentTime - this.lastVideoFrameTime >= 500 && this.mediaSendingEnabled) {
                  // Create black frame data (I420 format)
                  const width = window.initialData.videoFrameWidth, height = window.initialData.videoFrameHeight;
                  const yPlaneSize = width * height;
                  const uvPlaneSize = (width * height) / 4;
                  
                  const frameData = new Uint8Array(yPlaneSize + 2 * uvPlaneSize);
                  // Y plane (black = 0)
                  frameData.fill(0, 0, yPlaneSize);
                  // U and V planes (black = 128)
                  frameData.fill(128, yPlaneSize);
                  
                  // Fix: Math.floor() the milliseconds before converting to BigInt
                  const currentTimeMicros = BigInt(Math.floor(currentTime) * 1000);
                  this.sendVideo(currentTimeMicros, '0', width, height, frameData);
              }
          } catch (error) {
              console.error('Error in black frame timer:', error);
          }
      }, 250);
    }
  
    stopBlackFrameTimer() {
        if (this.blackFrameInterval) {
            clearInterval(this.blackFrameInterval);
            this.blackFrameInterval = null;
        }
    }
    */
  
    async enableMediaSending() {
        this.emitDiagnosticEvent('MediaSendingState', {
            state: 'enabling',
        });
        this.mediaSendingEnabled = true;
        window.receiverManager.startPollingReceivers();
        window.styleManager.start();
        window.callManager.syncParticipants();
        this.startVideoProbeLoop();
        if (window.initialData.sendEncodedVideoChunks) {
            await this.startVideoChunkRecording();
        }
        if (window.initialData.sendEncodedAudioChunks) {
            this.startAudioChunkRecording();
        }
        this.emitDiagnosticEvent('MediaSendingState', {
            state: 'enabled',
            send_encoded_video_chunks: !!window.initialData.sendEncodedVideoChunks,
            send_encoded_audio_chunks: !!window.initialData.sendEncodedAudioChunks,
        });
        // No longer need this because we're not using MediaStreamTrackProcessor's
        //this.startBlackFrameTimer();
    }

    async disableMediaSending() {
        this.emitDiagnosticEvent('MediaSendingState', {
            state: 'disabling',
            encoded_video_chunk_count: this.encodedVideoChunkCount,
            encoded_audio_chunk_count: this.encodedAudioChunkCount,
        });
        try {
            await this.stopVideoChunkRecording();
            await this.stopAudioChunkRecording();
            this.stopVideoProbeLoop();
            window.styleManager.stop();
            //window.fullCaptureManager.stop();
            // Give the media recorder a bit of time to send the final data
            await new Promise(resolve => setTimeout(resolve, 2000));
            this.mediaSendingEnabled = false;
            this.emitDiagnosticEvent('MediaSendingState', {
                state: 'disabled',
                encoded_video_chunk_count: this.encodedVideoChunkCount,
                encoded_audio_chunk_count: this.encodedAudioChunkCount,
            });
        } catch (error) {
            this.emitDiagnosticEvent('MediaSendingState', {
                state: 'disable_failed',
                error: error?.message || String(error),
            });
            throw error;
        }

        // No longer need this because we're not using MediaStreamTrackProcessor's
        //this.stopBlackFrameTimer();
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
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            realConsole?.error('WebSocket is not connected');
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
            at_ms: Date.now(),
            ...payload,
        });
    }

    startVideoProbeLoop() {
        if (this.videoProbeInterval) {
            return;
        }

        const runProbe = async () => {
            try {
                await this.runVideoProbe();
            } catch (error) {
                console.error('Video probe loop failed', error);
            }
        };

        runProbe();
        this.videoProbeInterval = setInterval(runProbe, 5000);
    }

    stopVideoProbeLoop() {
        if (this.videoProbeInterval) {
            clearInterval(this.videoProbeInterval);
            this.videoProbeInterval = null;
        }
    }

    async runVideoProbe() {
        if (!this.mediaSendingEnabled) {
            return;
        }

        this.sendJson({
            type: 'VideoProbeHeartbeat',
            at_ms: Date.now(),
            track_count: window.styleManager?.videoTracks?.size || 0,
        });

        await Promise.allSettled([
            this.probeReceiverStats(),
            this.probeDomMediaElements(),
        ]);

        const liveTrackEntries = Array.from(window.styleManager?.videoTracks?.values?.() || []).filter((item) => item.track?.readyState === 'live');
        for (const trackEntry of liveTrackEntries) {
            if (this.videoTrackProbePromises.has(trackEntry.track.id)) {
                continue;
            }
            const probePromise = this.probeVideoTrackEntry(trackEntry)
                .catch((error) => {
                    console.error('Video track probe failed', error);
                })
                .finally(() => {
                    this.videoTrackProbePromises.delete(trackEntry.track.id);
                });
            this.videoTrackProbePromises.set(trackEntry.track.id, probePromise);
        }
    }

    async probeReceiverStats() {
        const peerConnections = Array.from(window.__attendeePeerConnections || []);
        for (const peerConnection of peerConnections) {
            const receivers = typeof peerConnection.getReceivers === 'function' ? peerConnection.getReceivers() : [];
            for (const receiver of receivers) {
                if (receiver.track?.kind !== 'video') {
                    continue;
                }
                try {
                    const stats = await receiver.getStats();
                    const summary = {};
                    stats.forEach((report) => {
                        if (report.type === 'inbound-rtp' && !report.isRemote) {
                            summary.inbound = {
                                bytes_received: report.bytesReceived ?? null,
                                frames_decoded: report.framesDecoded ?? null,
                                frame_width: report.frameWidth ?? null,
                                frame_height: report.frameHeight ?? null,
                                decoder_implementation: report.decoderImplementation ?? null,
                                key_frames_decoded: report.keyFramesDecoded ?? null,
                            };
                        }
                        if (report.type === 'track') {
                            summary.track = {
                                frames_decoded: report.framesDecoded ?? null,
                                frames_received: report.framesReceived ?? null,
                                frame_width: report.frameWidth ?? null,
                                frame_height: report.frameHeight ?? null,
                            };
                        }
                    });
                    this.sendJson({
                        type: 'VideoProbeReceiverStats',
                        track_id: receiver.track?.id || null,
                        ready_state: receiver.track?.readyState || null,
                        stream_ids: receiver.track ? Array.from(window.styleManager?.videoTracks?.values?.() || []).filter((item) => item.track?.id === receiver.track.id).map((item) => item.streamId) : [],
                        stats: summary,
                    });
                } catch (error) {
                    this.sendJson({
                        type: 'VideoProbeReceiverStatsError',
                        track_id: receiver.track?.id || null,
                        message: error?.message || String(error),
                    });
                }
            }
        }
    }

    async probeDomMediaElements() {
        const centralElement = document.querySelector('[data-test-segment-type="central"]');
        const elements = Array.from(document.querySelectorAll('video, canvas')).filter((element) => (
            element !== this.videoChunkCanvas &&
            element !== this.videoChunkSourceCanvas &&
            element.isConnected
        ));

        const summaries = [];
        for (const element of elements.slice(0, 20)) {
            const rect = element.getBoundingClientRect();
            if (rect.width < 16 || rect.height < 16) {
                continue;
            }
            const isVideo = element.tagName === 'VIDEO';
            const width = isVideo ? element.videoWidth : element.width;
            const height = isVideo ? element.videoHeight : element.height;
            const pixelSummary = width > 0 && height > 0 ? summarizeDrawablePixels(element, width, height) : null;
            summaries.push({
                tag_name: element.tagName,
                id: element.id || null,
                class_name: element.className || null,
                rect: {
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                },
                media_size: {
                    width: width || null,
                    height: height || null,
                },
                ready_state: isVideo ? element.readyState : null,
                in_central_pane: !!centralElement && centralElement.contains(element),
                pixels: pixelSummary,
            });
        }

        this.sendJson({
            type: 'VideoProbeDomMedia',
            total_candidates: summaries.length,
            media_elements: summaries,
        });
    }

    async probeVideoTrackEntry(trackEntry) {
        const track = trackEntry?.track;
        if (!track || track.readyState !== 'live') {
            return;
        }

        const processorSummary = await this.probeVideoTrackViaProcessor(trackEntry);
        const hiddenVideoSummary = await this.probeVideoTrackViaHiddenVideo(trackEntry);
        this.sendJson({
            type: 'VideoProbeTrackSummary',
            track_id: track.id,
            stream_id: trackEntry.streamId || null,
            processor: processorSummary,
            hidden_video: hiddenVideoSummary,
        });
    }

    async probeVideoTrackViaProcessor(trackEntry) {
        const clone = trackEntry.track.clone();
        let reader = null;
        try {
            const processor = new MediaStreamTrackProcessor({ track: clone });
            reader = processor.readable.getReader();
            const frames = [];
            const deadline = Date.now() + 4000;
            while (frames.length < 5 && Date.now() < deadline) {
                const readResult = await Promise.race([
                    reader.read(),
                    new Promise((resolve) => setTimeout(() => resolve({ timeout: true }), 700)),
                ]);
                if (readResult?.timeout) {
                    continue;
                }
                const { value: frame, done } = readResult;
                if (done) {
                    break;
                }
                if (!frame) {
                    continue;
                }
                try {
                    const width = frame.displayWidth || frame.codedWidth || 0;
                    const height = frame.displayHeight || frame.codedHeight || 0;
                    frames.push({
                        width,
                        height,
                        timestamp: frame.timestamp ?? null,
                        pixels: width > 0 && height > 0 ? summarizeDrawablePixels(frame, width, height) : null,
                    });
                } finally {
                    frame.close();
                }
            }
            return {
                frame_count: frames.length,
                frames,
            };
        } catch (error) {
            return {
                error: error?.message || String(error),
            };
        } finally {
            if (reader) {
                try {
                    await reader.cancel();
                } catch (_) {}
                try {
                    reader.releaseLock();
                } catch (_) {}
            }
            try {
                clone.stop();
            } catch (_) {}
        }
    }

    async probeVideoTrackViaHiddenVideo(trackEntry) {
        const clone = trackEntry.track.clone();
        const video = document.createElement('video');
        video.autoplay = true;
        video.muted = true;
        video.playsInline = true;
        video.srcObject = new MediaStream([clone]);
        try {
            await video.play();
            await new Promise((resolve) => setTimeout(resolve, 500));
            const width = video.videoWidth || 0;
            const height = video.videoHeight || 0;
            return {
                ready_state: video.readyState,
                width,
                height,
                pixels: width > 0 && height > 0 ? summarizeDrawablePixels(video, width, height) : null,
            };
        } catch (error) {
            return {
                error: error?.message || String(error),
                ready_state: video.readyState,
            };
        } finally {
            video.srcObject = null;
            try {
                clone.stop();
            } catch (_) {}
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
        if (this.ws.readyState !== originalWebSocket.OPEN || !this.mediaSendingEnabled) {
            return;
        }
        try {
            const headerBuffer = new ArrayBuffer(4);
            const headerView = new DataView(headerBuffer);
            headerView.setInt32(0, WebSocketClient.MESSAGE_TYPES.ENCODED_AUDIO_CHUNK, true);
            this.ws.send(new Blob([headerBuffer, encodedAudioData]));
            this.encodedAudioChunkCount += 1;
            if (this.encodedAudioChunkCount <= 3 || this.encodedAudioChunkCount % 10 === 0) {
                this.emitDiagnosticEvent('EncodedMediaChunk', {
                    kind: 'audio',
                    chunk_count: this.encodedAudioChunkCount,
                    byte_length: encodedAudioData?.size || null,
                    mime_type: encodedAudioData?.type || null,
                });
            }
        } catch (error) {
            console.error('Error sending WebSocket audio chunk:', error);
        }
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

    sendEncodedMP4Chunk(encodedMP4Data) {
        if (this.ws.readyState !== originalWebSocket.OPEN || !this.mediaSendingEnabled) {
            return;
        }
        try {
            const headerBuffer = new ArrayBuffer(4);
            const headerView = new DataView(headerBuffer);
            headerView.setInt32(0, WebSocketClient.MESSAGE_TYPES.ENCODED_MP4_CHUNK, true);
            this.ws.send(new Blob([headerBuffer, encodedMP4Data]));
            this.encodedVideoChunkCount += 1;
            if (this.encodedVideoChunkCount <= 3 || this.encodedVideoChunkCount % 10 === 0) {
                this.emitDiagnosticEvent('EncodedMediaChunk', {
                    kind: 'video',
                    chunk_count: this.encodedVideoChunkCount,
                    byte_length: encodedMP4Data?.size || null,
                    mime_type: encodedMP4Data?.type || null,
                    source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                    track_id: this.videoChunkSourceTrackId,
                    stream_id: this.videoChunkSourceStreamId,
                    dom_source_key: this.videoChunkDomSourceKey,
                });
            }
        } catch (error) {
            console.error('Error sending WebSocket video chunk:', error);
        }
    }

    ensureVideoChunkCanvas() {
        if (!this.videoChunkCanvas) {
            this.videoChunkCanvas = document.createElement('canvas');
            this.videoChunkCanvas.width = window.initialData.videoFrameWidth || 1280;
            this.videoChunkCanvas.height = window.initialData.videoFrameHeight || 720;
            this.videoChunkCanvasCtx = this.videoChunkCanvas.getContext('2d');
        }
        if (!this.videoChunkSourceCanvas) {
            this.videoChunkSourceCanvas = document.createElement('canvas');
            this.videoChunkSourceCanvas.width = this.videoChunkCanvas.width;
            this.videoChunkSourceCanvas.height = this.videoChunkCanvas.height;
            this.videoChunkSourceCanvasCtx = this.videoChunkSourceCanvas.getContext('2d');
        }
    }

    stopVideoChunkCanvasLoop() {
        if (this.videoChunkAnimationFrame) {
            cancelAnimationFrame(this.videoChunkAnimationFrame);
            this.videoChunkAnimationFrame = null;
        }
    }

    cleanupVideoChunkSourceTrack() {
        this.videoChunkSourceGeneration = (this.videoChunkSourceGeneration || 0) + 1;
        if (this.videoChunkFrameReader) {
            try {
                this.videoChunkFrameReader.cancel().catch(() => {});
            } catch (error) {
                console.warn('Video chunk frame reader cleanup failed', error);
            }
            this.videoChunkFrameReader = null;
        }
        if (this.videoChunkSourceTrackClone) {
            try {
                this.videoChunkSourceTrackClone.stop();
            } catch (error) {
                console.warn('Video chunk source track cleanup failed', error);
            }
            this.videoChunkSourceTrackClone = null;
        }
        this.videoChunkSourceTrackId = null;
        this.videoChunkSourceStreamId = null;
        this.videoChunkSourceTrackSettings = null;
    }

    cleanupVideoChunkDomSourceElement() {
        this.videoChunkDomSourceElement = null;
        this.videoChunkDomSourceKey = null;
    }

    cleanupVideoChunkSource() {
        this.cleanupVideoChunkDomSourceElement();
        this.cleanupVideoChunkSourceTrack();
    }

    trackVideoChunkHealthyFrame(width, height) {
        const now = performance.now();
        this.videoChunkLastHealthyFrameAt = now;
        const dimensionsKey = `${width}x${height}`;
        if (!this.videoChunkFirstFrameLoggedAt) {
            this.videoChunkFirstFrameLoggedAt = now;
            console.log(
                'Video chunk source received first frame',
                JSON.stringify({
                    source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                    track_id: this.videoChunkSourceTrackId,
                    stream_id: this.videoChunkSourceStreamId,
                    dom_source_key: this.videoChunkDomSourceKey,
                    width: width,
                    height: height,
                }),
            );
            this.emitDiagnosticEvent('RecordingVideoFrameHealth', {
                state: 'first_healthy_frame',
                source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                track_id: this.videoChunkSourceTrackId,
                stream_id: this.videoChunkSourceStreamId,
                dom_source_key: this.videoChunkDomSourceKey,
                width: width,
                height: height,
            });
        } else if (this.videoChunkLastFrameDimensionsKey !== dimensionsKey) {
            console.log(
                'Video chunk source frame dimensions changed',
                JSON.stringify({
                    source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                    track_id: this.videoChunkSourceTrackId,
                    stream_id: this.videoChunkSourceStreamId,
                    dom_source_key: this.videoChunkDomSourceKey,
                    width: width,
                    height: height,
                }),
            );
            this.emitDiagnosticEvent('RecordingVideoFrameHealth', {
                state: 'dimensions_changed',
                source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                track_id: this.videoChunkSourceTrackId,
                stream_id: this.videoChunkSourceStreamId,
                dom_source_key: this.videoChunkDomSourceKey,
                width: width,
                height: height,
            });
        }
        this.videoChunkLastFrameDimensionsKey = dimensionsKey;
        if (this.videoChunkStallLogState !== 'healthy') {
            console.log(
                'Video chunk source is healthy',
                JSON.stringify({
                    source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                    track_id: this.videoChunkSourceTrackId,
                    stream_id: this.videoChunkSourceStreamId,
                    dom_source_key: this.videoChunkDomSourceKey,
                    width: width,
                    height: height,
                }),
            );
            this.emitDiagnosticEvent('RecordingVideoFrameHealth', {
                state: 'healthy',
                source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                track_id: this.videoChunkSourceTrackId,
                stream_id: this.videoChunkSourceStreamId,
                dom_source_key: this.videoChunkDomSourceKey,
                width: width,
                height: height,
            });
            this.videoChunkStallLogState = 'healthy';
        }
    }

    async startVideoChunkSourcePump(trackEntry) {
        if (!trackEntry?.track || trackEntry.track.readyState !== 'live') {
            return;
        }

        this.cleanupVideoChunkSource();
        this.ensureVideoChunkCanvas();

        const sourceTrack = trackEntry.track;
        const sourceStreamId = trackEntry.streamId || null;
        const sourceTrackClone = sourceTrack.clone();
        const sourceGeneration = (this.videoChunkSourceGeneration || 0) + 1;
        this.videoChunkSourceGeneration = sourceGeneration;
        this.videoChunkSourceTrackClone = sourceTrackClone;
        this.videoChunkSourceTrackId = sourceTrack.id;
        this.videoChunkSourceStreamId = sourceStreamId;
        this.videoChunkSourceTrackSettings = typeof sourceTrack.getSettings === 'function' ? sourceTrack.getSettings() : null;
        this.videoChunkLastHealthyFrameAt = null;
        this.videoChunkLastFrameDimensionsKey = null;
        this.videoChunkStallLogState = null;

        console.log(
            'Video chunk source selected',
            JSON.stringify({
                track_id: this.videoChunkSourceTrackId,
                stream_id: this.videoChunkSourceStreamId,
                kind: sourceTrack.kind,
                ready_state: sourceTrack.readyState,
                settings: this.videoChunkSourceTrackSettings,
            }),
        );
        this.emitDiagnosticEvent('RecordingVideoSourceSelected', {
            source_type: 'track_processor',
            track_id: this.videoChunkSourceTrackId,
            stream_id: this.videoChunkSourceStreamId,
            kind: sourceTrack.kind,
            ready_state: sourceTrack.readyState,
            settings: this.videoChunkSourceTrackSettings,
        });

        let processor;
        try {
            processor = new MediaStreamTrackProcessor({ track: sourceTrackClone });
        } catch (error) {
            console.error('Video chunk source processor setup failed', error);
            return;
        }

        const reader = processor.readable.getReader();
        this.videoChunkFrameReader = reader;

        try {
            while (this.videoChunkSourceGeneration === sourceGeneration) {
                const { value: frame, done } = await reader.read();
                if (done) {
                    break;
                }
                if (!frame) {
                    continue;
                }

                try {
                    const width = frame.displayWidth || frame.codedWidth || 0;
                    const height = frame.displayHeight || frame.codedHeight || 0;
                    if (!width || !height) {
                        continue;
                    }

                    const sourceCanvas = this.videoChunkSourceCanvas;
                    const sourceCtx = this.videoChunkSourceCanvasCtx;
                    if (!sourceCanvas || !sourceCtx) {
                        continue;
                    }

                    if (sourceCanvas.width !== width || sourceCanvas.height !== height) {
                        sourceCanvas.width = width;
                        sourceCanvas.height = height;
                    }

                    sourceCtx.drawImage(frame, 0, 0, width, height);
                    this.trackVideoChunkHealthyFrame(width, height);
                } catch (error) {
                    console.error('Video chunk frame pump failed to render frame', error);
                } finally {
                    frame.close();
                }
            }
        } catch (error) {
            if (this.videoChunkSourceGeneration === sourceGeneration) {
                console.error(
                    'Video chunk source pump failed',
                    JSON.stringify({
                        track_id: this.videoChunkSourceTrackId,
                        stream_id: this.videoChunkSourceStreamId,
                        message: error?.message || String(error),
                    }),
                );
            }
        } finally {
            if (this.videoChunkFrameReader === reader) {
                this.videoChunkFrameReader = null;
            }
            try {
                reader.releaseLock();
            } catch (error) {
                console.warn('Video chunk frame reader release failed', error);
            }
        }
    }

    syncVideoChunkDomSourceElement() {
        const nextElement = window.styleManager?.getRenderableElementForRecording?.();
        if (!nextElement) {
            if (this.videoChunkLastLoggedTrackInfo !== 'missing_dom_element') {
                console.warn('Video chunk recording has no renderable central media element');
                this.videoChunkLastLoggedTrackInfo = 'missing_dom_element';
                this.videoChunkMissingSourceReason = 'missing_dom_element';
                this.emitDiagnosticEvent('RecordingVideoSourceUnavailable', {
                    reason: 'missing_dom_element',
                });
            }
            this.cleanupVideoChunkDomSourceElement();
            return false;
        }

        const nextKey = [
            nextElement.tagName,
            nextElement.id || '',
            nextElement.className || '',
            nextElement.srcObject?.id || '',
            nextElement.currentSrc || '',
        ].join('|');

        if (this.videoChunkDomSourceElement === nextElement && this.videoChunkDomSourceKey === nextKey) {
            return true;
        }

        this.cleanupVideoChunkSource();
        this.videoChunkDomSourceElement = nextElement;
        this.videoChunkDomSourceKey = nextKey;
        this.videoChunkLastLoggedTrackInfo = nextKey;
        this.videoChunkSourceTrackSettings = null;
        console.log(
            'Video chunk DOM source selected',
            JSON.stringify({
                dom_source_key: this.videoChunkDomSourceKey,
                tag_name: nextElement.tagName,
                class_name: nextElement.className || null,
                id: nextElement.id || null,
            }),
        );
        this.videoChunkMissingSourceReason = null;
        this.emitDiagnosticEvent('RecordingVideoSourceSelected', {
            source_type: 'dom_element',
            dom_source_key: this.videoChunkDomSourceKey,
            tag_name: nextElement.tagName,
            class_name: nextElement.className || null,
            id: nextElement.id || null,
        });
        return true;
    }

    syncVideoChunkSourceTrack() {
        this.ensureVideoChunkCanvas();
        if (this.syncVideoChunkDomSourceElement()) {
            return;
        }
        const nextTrackEntry = window.styleManager?.getVideoTrackEntryForRecording?.();
        if (!nextTrackEntry?.track || nextTrackEntry.track.readyState !== 'live') {
            if (this.videoChunkLastLoggedTrackInfo !== 'missing_track') {
                console.warn('Video chunk recording has no live source track');
                this.videoChunkLastLoggedTrackInfo = 'missing_track';
                this.videoChunkMissingSourceReason = 'missing_track';
                this.emitDiagnosticEvent('RecordingVideoSourceUnavailable', {
                    reason: 'missing_track',
                });
            }
            this.cleanupVideoChunkSource();
            return;
        }
        if (this.videoChunkSourceTrackId === nextTrackEntry.track.id) {
            return;
        }
        this.videoChunkMissingSourceReason = null;
        this.videoChunkLastLoggedTrackInfo = nextTrackEntry.track.id;
        this.startVideoChunkSourcePump(nextTrackEntry).catch((error) => {
            console.error('Video chunk source pump start failed', error);
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
                this.trackVideoChunkHealthyFrame(sourceWidth, sourceHeight);
                this.videoChunkAnimationFrame = requestAnimationFrame(this.drawVideoChunkFrame);
                return;
            }
        }

        const sourceCanvas = this.videoChunkSourceCanvas;
        const lastHealthyFrameAgeMs = this.videoChunkLastHealthyFrameAt == null
            ? Number.POSITIVE_INFINITY
            : performance.now() - this.videoChunkLastHealthyFrameAt;
        if (sourceCanvas && this.videoChunkLastHealthyFrameAt != null && lastHealthyFrameAgeMs < 3000) {
            if (canvas.width !== sourceCanvas.width || canvas.height !== sourceCanvas.height) {
                canvas.width = sourceCanvas.width;
                canvas.height = sourceCanvas.height;
            }
            ctx.drawImage(sourceCanvas, 0, 0, canvas.width, canvas.height);
            if (this.videoChunkStallLogState && this.videoChunkStallLogState !== 'healthy') {
                console.log(
                    'Video chunk recording recovered from stalled frames',
                    JSON.stringify({
                        track_id: this.videoChunkSourceTrackId,
                        stream_id: this.videoChunkSourceStreamId,
                        last_healthy_frame_age_ms: Math.round(lastHealthyFrameAgeMs),
                    }),
                );
                this.videoChunkStallLogState = 'healthy';
            }
        } else {
            if (this.videoChunkStallLogState !== 'stalled') {
                console.warn(
                    'Video chunk recording has no recent renderable frames; drawing black frame',
                    JSON.stringify({
                        track_id: this.videoChunkSourceTrackId,
                        stream_id: this.videoChunkSourceStreamId,
                        last_healthy_frame_age_ms: Number.isFinite(lastHealthyFrameAgeMs) ? Math.round(lastHealthyFrameAgeMs) : null,
                        track_settings: this.videoChunkSourceTrackSettings,
                    }),
                );
                this.emitDiagnosticEvent('RecordingVideoFrameHealth', {
                    state: 'stalled',
                    track_id: this.videoChunkSourceTrackId,
                    stream_id: this.videoChunkSourceStreamId,
                    last_healthy_frame_age_ms: Number.isFinite(lastHealthyFrameAgeMs) ? Math.round(lastHealthyFrameAgeMs) : null,
                    track_settings: this.videoChunkSourceTrackSettings,
                    source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                    missing_source_reason: this.videoChunkMissingSourceReason,
                });
                this.videoChunkStallLogState = 'stalled';
            }
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
            return;
        }

        this.ensureVideoChunkCanvas();
        this.syncVideoChunkSourceTrack();
        if (!this.videoChunkDomSourceElement && !this.videoChunkSourceTrackClone) {
            console.warn('No meeting video source available for video chunk recording yet; starting canvas recorder with black frames');
        }
        this.videoChunkFirstFrameLoggedAt = null;
        this.videoChunkLastFrameDimensionsKey = null;
        this.videoChunkStallLogState = null;

        const recorderStream = this.videoChunkCanvas.captureStream(30);
        const meetingAudioStream = window.styleManager?.getMeetingAudioStream?.();
        const audioTrack = meetingAudioStream?.getAudioTracks?.()[0];
        if (audioTrack) {
            recorderStream.addTrack(audioTrack.clone());
        }

        const selectedMimeType = this.getPreferredVideoChunkMimeType();
        console.log(
            'Starting video chunk recording',
            JSON.stringify({
                mime_type: selectedMimeType || 'browser-default',
                source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                track_id: this.videoChunkSourceTrackId,
                stream_id: this.videoChunkSourceStreamId,
                dom_source_key: this.videoChunkDomSourceKey,
                canvas: {
                    width: this.videoChunkCanvas?.width || null,
                    height: this.videoChunkCanvas?.height || null,
                },
            }),
        );
        this.emitDiagnosticEvent('RecordingChunkLifecycle', {
            event: 'video_recording_started',
            mime_type: selectedMimeType || 'browser-default',
            source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
            track_id: this.videoChunkSourceTrackId,
            stream_id: this.videoChunkSourceStreamId,
            dom_source_key: this.videoChunkDomSourceKey,
            canvas: {
                width: this.videoChunkCanvas?.width || null,
                height: this.videoChunkCanvas?.height || null,
            },
        });
        this.videoChunkRecorder = selectedMimeType
            ? new MediaRecorder(recorderStream, { mimeType: selectedMimeType })
            : new MediaRecorder(recorderStream);
        this.videoChunkRecorder.ondataavailable = (event) => {
            if (event.data && event.data.size > 0) {
                console.log('Video chunk recorder produced chunk', event.data.type || selectedMimeType || 'unknown', event.data.size);
                this.sendEncodedMP4Chunk(event.data);
            }
        };
        this.stopVideoChunkCanvasLoop();
        this.videoChunkAnimationFrame = requestAnimationFrame(this.drawVideoChunkFrame);
        this.videoChunkRecorder.start();
        this.videoChunkFlushInterval = setInterval(() => {
            try {
                if (this.videoChunkRecorder?.state === 'recording') {
                    this.videoChunkRecorder.requestData();
                }
            } catch (error) {
                console.warn('Video chunk recorder requestData failed', error);
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
            this.cleanupVideoChunkSource();
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
        console.log(
            'Stopping video chunk recording',
            JSON.stringify({
                source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
                track_id: this.videoChunkSourceTrackId,
                stream_id: this.videoChunkSourceStreamId,
                dom_source_key: this.videoChunkDomSourceKey,
                last_healthy_frame_age_ms: this.videoChunkLastHealthyFrameAt == null
                    ? null
                    : Math.round(performance.now() - this.videoChunkLastHealthyFrameAt),
                saw_first_frame: this.videoChunkFirstFrameLoggedAt != null,
            }),
        );
        this.emitDiagnosticEvent('RecordingChunkLifecycle', {
            event: 'video_recording_stopped',
            source_type: this.videoChunkDomSourceElement ? 'dom_element' : 'track_processor',
            track_id: this.videoChunkSourceTrackId,
            stream_id: this.videoChunkSourceStreamId,
            dom_source_key: this.videoChunkDomSourceKey,
            last_healthy_frame_age_ms: this.videoChunkLastHealthyFrameAt == null
                ? null
                : Math.round(performance.now() - this.videoChunkLastHealthyFrameAt),
            saw_first_frame: this.videoChunkFirstFrameLoggedAt != null,
            encoded_video_chunk_count: this.encodedVideoChunkCount,
        });
        this.cleanupVideoChunkSource();
    }

    startAudioChunkRecording() {
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'start_audio_chunk_recording_enter',
            has_existing_recorder: !!this.audioChunkRecorder,
            existing_recorder_state: this.audioChunkRecorder?.state || null,
        });
        if (this.audioChunkRecorder && this.audioChunkRecorder.state !== 'inactive') {
            return;
        }
        const audioStream = window.styleManager?.getMeetingAudioStream?.();
        if (!audioStream || audioStream.getAudioTracks().length === 0) {
            console.warn('No meeting audio stream available for audio chunk recording');
            this.emitDiagnosticEvent('AudioRecordingState', {
                event: 'missing_audio_stream',
                has_audio_stream: !!audioStream,
                track_count: audioStream?.getAudioTracks?.().length || 0,
            });
            return;
        }
        const audioTracks = audioStream.getAudioTracks();
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'got_audio_stream',
            stream_active: audioStream.active,
            track_count: audioTracks.length,
            tracks: audioTracks.map((track) => ({
                id: track.id,
                enabled: track.enabled,
                muted: track.muted,
                ready_state: track.readyState,
                label: track.label || null,
            })),
        });
        for (const track of audioTracks) {
            track.addEventListener('mute', () => {
                this.emitDiagnosticEvent('AudioRecordingState', {
                    event: 'track_muted',
                    track_id: track.id,
                    ready_state: track.readyState,
                    enabled: track.enabled,
                    muted: track.muted,
                });
            });
            track.addEventListener('unmute', () => {
                this.emitDiagnosticEvent('AudioRecordingState', {
                    event: 'track_unmuted',
                    track_id: track.id,
                    ready_state: track.readyState,
                    enabled: track.enabled,
                    muted: track.muted,
                });
            });
            track.addEventListener('ended', () => {
                this.emitDiagnosticEvent('AudioRecordingState', {
                    event: 'track_ended',
                    track_id: track.id,
                    ready_state: track.readyState,
                    enabled: track.enabled,
                    muted: track.muted,
                });
            });
        }
        const preferredMimeTypes = ['audio/webm;codecs=opus', 'audio/webm'];
        const selectedMimeType = preferredMimeTypes.find((mime) => window.MediaRecorder && MediaRecorder.isTypeSupported(mime));
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'media_recorder_capability',
            media_recorder_available: !!window.MediaRecorder,
            preferred_mime_types: preferredMimeTypes,
            supported_mime_types: preferredMimeTypes.filter((mime) => window.MediaRecorder && MediaRecorder.isTypeSupported(mime)),
            selected_mime_type: selectedMimeType || null,
        });
        try {
            this.audioChunkRecorder = selectedMimeType ? new MediaRecorder(audioStream, { mimeType: selectedMimeType }) : new MediaRecorder(audioStream);
        } catch (error) {
            this.emitDiagnosticEvent('AudioRecordingState', {
                event: 'media_recorder_constructor_failed',
                selected_mime_type: selectedMimeType || null,
                message: error?.message || String(error),
            });
            throw error;
        }
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'media_recorder_constructed',
            selected_mime_type: selectedMimeType || null,
            recorder_state: this.audioChunkRecorder?.state || null,
            recorder_mime_type: this.audioChunkRecorder?.mimeType || null,
        });
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'resolving_audio_chunk_mime_type',
            recorder_mime_type: this.audioChunkRecorder?.mimeType || null,
            selected_mime_type: selectedMimeType || null,
        });
        const audioChunkMimeType = this.audioChunkRecorder.mimeType || selectedMimeType || 'audio/webm';
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'resolved_audio_chunk_mime_type',
            audio_chunk_mime_type: audioChunkMimeType,
            recorder_mime_type: this.audioChunkRecorder?.mimeType || null,
        });
        try {
            this.sendJson({
                type: 'RecordingChunkFormat',
                kind: 'audio',
                mimeType: audioChunkMimeType,
                extension: audioChunkMimeType.includes('mp4') ? 'm4a' : 'webm',
            });
        } catch (error) {
            this.emitDiagnosticEvent('AudioRecordingState', {
                event: 'recording_chunk_format_send_failed',
                audio_chunk_mime_type: audioChunkMimeType,
                message: error?.message || String(error),
            });
            throw error;
        }
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'recording_chunk_format_sent',
            audio_chunk_mime_type: audioChunkMimeType,
        });
        this.emitDiagnosticEvent('RecordingChunkLifecycle', {
            event: 'audio_recording_started',
            mime_type: audioChunkMimeType,
            track_count: audioTracks.length,
        });
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'media_recorder_created',
            mime_type: audioChunkMimeType,
            recorder_state: this.audioChunkRecorder.state,
            track_count: audioTracks.length,
            tracks: audioTracks.map((track) => ({
                id: track.id,
                enabled: track.enabled,
                muted: track.muted,
                ready_state: track.readyState,
                label: track.label || null,
            })),
        });
        this.audioChunkRecorder.onstart = () => {
            this.emitDiagnosticEvent('AudioRecordingState', {
                event: 'media_recorder_started',
                recorder_state: this.audioChunkRecorder?.state || null,
            });
        };
        this.audioChunkRecorder.onstop = () => {
            this.emitDiagnosticEvent('AudioRecordingState', {
                event: 'media_recorder_stopped',
                recorder_state: this.audioChunkRecorder?.state || null,
                encoded_audio_chunk_count: this.encodedAudioChunkCount,
                zero_size_chunk_count: this.audioChunkZeroSizeCount,
                request_count: this.audioChunkRequestCount,
            });
        };
        this.audioChunkRecorder.onerror = (event) => {
            this.emitDiagnosticEvent('AudioRecordingState', {
                event: 'media_recorder_error',
                message: event?.error?.message || event?.message || String(event),
            });
        };
        this.audioChunkRecorder.ondataavailable = (event) => {
            this.emitDiagnosticEvent('AudioRecordingState', {
                event: 'dataavailable_fired',
                byte_length: event?.data?.size || 0,
                mime_type: event?.data?.type || null,
                recorder_state: this.audioChunkRecorder?.state || null,
            });
            if (event.data && event.data.size > 0) {
                this.sendEncodedAudioChunk(event.data);
                return;
            }
            this.audioChunkZeroSizeCount += 1;
            this.emitDiagnosticEvent('AudioRecordingState', {
                event: 'zero_size_chunk',
                zero_size_chunk_count: this.audioChunkZeroSizeCount,
                mime_type: event?.data?.type || null,
                recorder_state: this.audioChunkRecorder?.state || null,
            });
        };
        this.emitDiagnosticEvent('AudioRecordingState', {
            event: 'media_recorder_start_called',
            timeslice_ms: null,
            recorder_state: this.audioChunkRecorder.state,
        });
        this.audioChunkRecorder.start();
        this.audioChunkFlushInterval = setInterval(() => {
            try {
                if (this.audioChunkRecorder?.state === 'recording') {
                    this.audioChunkRequestCount += 1;
                    if (this.audioChunkRequestCount <= 3 || this.audioChunkRequestCount % 10 === 0) {
                        this.emitDiagnosticEvent('AudioRecordingState', {
                            event: 'request_data',
                            request_count: this.audioChunkRequestCount,
                            recorder_state: this.audioChunkRecorder?.state || null,
                            encoded_audio_chunk_count: this.encodedAudioChunkCount,
                        });
                    }
                    this.audioChunkRecorder.requestData();
                }
            } catch (error) {
                console.warn('Audio chunk recorder requestData failed', error);
                this.emitDiagnosticEvent('AudioRecordingState', {
                    event: 'request_data_failed',
                    request_count: this.audioChunkRequestCount,
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
                this.emitDiagnosticEvent('AudioRecordingState', {
                    event: 'final_request_data',
                    request_count: this.audioChunkRequestCount,
                    recorder_state: recorder?.state || null,
                });
                recorder.requestData();
            } catch (error) {
                console.warn('Final audio chunk requestData failed', error);
                this.emitDiagnosticEvent('AudioRecordingState', {
                    event: 'final_request_data_failed',
                    request_count: this.audioChunkRequestCount,
                    message: error?.message || String(error),
                });
            }
            recorder.stop();
        });
        this.audioChunkRecorder = null;
        this.emitDiagnosticEvent('RecordingChunkLifecycle', {
            event: 'audio_recording_stopped',
            encoded_audio_chunk_count: this.encodedAudioChunkCount,
        });
    }

    sendMixedAudio(timestamp, audioData) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            realConsole?.error('WebSocket is not connected for audio send', this.ws.readyState);
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
            realConsole?.error('Error sending WebSocket audio message:', error);
        }
    }
  
    sendPerParticipantAudio(participantId, audioData) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            realConsole?.error('WebSocket is not connected for per participant audio send', this.ws.readyState);
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
            realConsole?.error('Error sending WebSocket audio message:', error);
        }
      }

    sendAudio(timestamp, streamId, audioData) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            realConsole?.error('WebSocket is not connected for audio send', this.ws.readyState);
            return;
        }
  
        if (!this.mediaSendingEnabled) {
          return;
        }
  
        try {
            // Create final message: type (4 bytes) + timestamp (8 bytes) + audio data
            const message = new Uint8Array(4 + 8 + 4 + audioData.buffer.byteLength);
            const dataView = new DataView(message.buffer);
            
            // Set message type (3 for AUDIO)
            dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.AUDIO, true);
            
            // Set timestamp as BigInt64
            dataView.setBigInt64(4, BigInt(timestamp), true);
  
            // Set streamId length and bytes
            dataView.setInt32(12, streamId, true);
  
            // Copy audio data after type and timestamp
            message.set(new Uint8Array(audioData.buffer), 16);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            realConsole?.error('Error sending WebSocket audio message:', error);
        }
    }
  
    sendVideo(timestamp, streamId, width, height, videoData) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            console.error('WebSocket is not connected for video send', this.ws.readyState);
            return;
        }
  
        if (!this.mediaSendingEnabled) {
          return;
        }
        
        this.lastVideoFrameTime = performance.now();
  
        try {
            // Convert streamId to UTF-8 bytes
            const streamIdBytes = new TextEncoder().encode(streamId);
            
            // Create final message: type (4 bytes) + timestamp (8 bytes) + streamId length (4 bytes) + 
            // streamId bytes + width (4 bytes) + height (4 bytes) + video data
            const message = new Uint8Array(4 + 8 + 4 + streamIdBytes.length + 4 + 4 + videoData.buffer.byteLength);
            const dataView = new DataView(message.buffer);
            
            // Set message type (2 for VIDEO)
            dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.VIDEO, true);
            
            // Set timestamp as BigInt64
            dataView.setBigInt64(4, BigInt(timestamp), true);
  
            // Set streamId length and bytes
            dataView.setInt32(12, streamIdBytes.length, true);
            message.set(streamIdBytes, 16);
  
            // Set width and height
            const streamIdOffset = 16 + streamIdBytes.length;
            dataView.setInt32(streamIdOffset, width, true);
            dataView.setInt32(streamIdOffset + 4, height, true);
  
            // Copy video data after headers
            message.set(new Uint8Array(videoData.buffer), streamIdOffset + 8);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            console.error('Error sending WebSocket video message:', error);
        }
    }
  }

class WebSocketInterceptor {
    constructor(callbacks = {}) {
        this.originalWebSocket = window.WebSocket;
        this.callbacks = {
            onSend: callbacks.onSend || (() => {}),
            onMessage: callbacks.onMessage || (() => {}),
            onOpen: callbacks.onOpen || (() => {}),
            onClose: callbacks.onClose || (() => {}),
            onError: callbacks.onError || (() => {})
        };
        
        window.WebSocket = this.createWebSocketProxy();
    }

    createWebSocketProxy() {
        const OriginalWebSocket = this.originalWebSocket;
        const callbacks = this.callbacks;
        
        return function(url, protocols) {
            const ws = new OriginalWebSocket(url, protocols);
            
            // Intercept send
            const originalSend = ws.send;
            ws.send = function(data) {
                try {
                    callbacks.onSend({
                        url,
                        data,
                        ws
                    });
                } catch (error) {
                    realConsole?.log('Error in WebSocket send callback:');
                    realConsole?.log(error);
                }
                
                return originalSend.apply(ws, arguments);
            };
            
            // Intercept onmessage
            ws.addEventListener('message', function(event) {
                try {
                    callbacks.onMessage({
                        url,
                        data: event.data,
                        event,
                        ws
                    });
                } catch (error) {
                    realConsole?.log('Error in WebSocket message callback:');
                    realConsole?.log(error);
                }
            });
            
            // Intercept connection events
            ws.addEventListener('open', (event) => {
                callbacks.onOpen({ url, event, ws });
            });
            
            ws.addEventListener('close', (event) => {
                callbacks.onClose({ 
                    url, 
                    code: event.code, 
                    reason: event.reason,
                    event,
                    ws 
                });
            });
            
            ws.addEventListener('error', (event) => {
                callbacks.onError({ url, event, ws });
            });
            
            return ws;
        };
    }
}

function decodeWebSocketBody(encodedData) {
    const byteArray = Uint8Array.from(atob(encodedData), c => c.charCodeAt(0));
    return JSON.parse(pako.inflate(byteArray, { to: "string" }));
}

function syncVirtualStreamsFromParticipant(participant) {
    if (participant.state === 'inactive') {
        virtualStreamToPhysicalStreamMappingManager.removeVirtualStreamsForParticipant(participant.details?.id);
        return;
    }

    const mediaStreams = [];
    
    // Check if participant has endpoints
    if (participant.endpoints) {
        // Iterate through all endpoints
        Object.values(participant.endpoints).forEach(endpoint => {
            // Check if endpoint has call and mediaStreams
            if (endpoint.call && Array.isArray(endpoint.call.mediaStreams)) {
                // Add all mediaStreams from this endpoint to our array
                mediaStreams.push(...endpoint.call.mediaStreams);
            }
        });
    }
    
    for (const mediaStream of mediaStreams) {
        const isScreenShare = mediaStream.type === 'applicationsharing-video';
        const isWebcam = mediaStream.type === 'video';
        const isActive = mediaStream.direction === 'sendrecv' || mediaStream.direction === 'sendonly';
        virtualStreamToPhysicalStreamMappingManager.upsertVirtualStream(
            {...mediaStream, participant: {displayName: participant.details?.displayName, id: participant.details?.id}, isScreenShare, isWebcam, isActive}
        );
    }
}

function extractCallIdFromEventDataObject(eventDataObject) {
    return eventDataObject?.headers?.["X-Microsoft-Skype-Chain-ID"];
}

function handleRosterUpdate(eventDataObject) {
    try {
        const decodedBody = decodeWebSocketBody(eventDataObject.body);
        realConsole?.log('handleRosterUpdate decodedBody', decodedBody);
        // Teams includes a user with no display name. Not sure what this user is but we don't want to sync that user.
        const participants = Object.values(decodedBody.participants).filter(participant => participant.details?.displayName);
        const callId = extractCallIdFromEventDataObject(eventDataObject);
        for (const participant of participants) {
            const participantWithCallId = {
                ...participant,
                callId: callId
            };
            window.userManager.singleUserSynced(participantWithCallId);
            syncVirtualStreamsFromParticipant(participant);
        }
    } catch (error) {
        realConsole?.error('Error handling roster update:');
        realConsole?.error(error);
    }
}

function handleConversationEnd(eventDataObject) {

    let eventDataObjectBody = {};
    try {
        eventDataObjectBody = JSON.parse(eventDataObject.body);
    } catch (error) {
        realConsole?.error('Error parsing eventDataObject.body:', error);

        try {
            eventDataObjectBody = decodeWebSocketBody(eventDataObject.body);
        } catch (error) {
            realConsole?.error('Error decoding eventDataObject.body:', error);
        }
    }

    realConsole?.log('handleConversationEnd, eventDataObjectBody', eventDataObjectBody);
    window.ws?.sendJson({
        type: 'ConversationEndPayload',
        body: eventDataObjectBody
    });

    const subCode = eventDataObjectBody?.subCode;
    const subCodeValueForDeniedRequestToJoin = 5854;
    const subCodeForAnonymousJoinDisabledForTenantByPolicy = 5723;

    if (subCode === subCodeValueForDeniedRequestToJoin)
    {
        // For now this won't do anything, but good to have it in our logs. In the future, this should probably be the source of truth for these things, instead of the UI inspection.
        window.ws?.sendJson({
            type: 'MeetingStatusChange',
            change: 'request_to_join_denied'
        });
        return;
    }

    if (subCode === subCodeForAnonymousJoinDisabledForTenantByPolicy)
    {
        // For now this won't do anything, but good to have it in our logs. In the future, this should probably be the source of truth for these things, instead of the UI inspection.
        window.ws?.sendJson({
            type: 'MeetingStatusChange',
            change: 'anonymous_join_disabled_for_tenant_by_policy'
        });
        return;
    }

    realConsole?.log('handleConversationEnd, sending meeting ended message');
    window.ws?.emitDiagnosticEvent?.('MeetingEndLifecycle', {
        event: 'conversation_end_received',
        sub_code: subCode ?? null,
    });
    try {
        const disableResult = window.ws?.disableMediaSending?.();
        if (disableResult && typeof disableResult.catch === 'function') {
            disableResult.catch((error) => {
                realConsole?.warn('handleConversationEnd failed to disable media sending', error);
                window.ws?.emitDiagnosticEvent?.('MeetingEndLifecycle', {
                    event: 'disable_media_sending_failed',
                    error: error?.message || String(error),
                });
            });
        }
    } catch (error) {
        realConsole?.warn('handleConversationEnd failed to disable media sending', error);
        window.ws?.emitDiagnosticEvent?.('MeetingEndLifecycle', {
            event: 'disable_media_sending_failed',
            error: error?.message || String(error),
        });
    }
    window.ws?.sendJson({
        type: 'MeetingStatusChange',
        change: 'meeting_ended'
    });
    window.ws?.emitDiagnosticEvent?.('MeetingEndLifecycle', {
        event: 'meeting_status_change_sent',
        change: 'meeting_ended',
    });
}

const originalWebSocket = window.WebSocket;
// Example usage:
const wsInterceptor = new WebSocketInterceptor({
    /*
    onSend: ({ url, data }) => {
        if (url.startsWith('ws://localhost:8097'))
            return;
        
        //realConsole?.log('websocket onSend', url, data);        
    },
    */
    onMessage: ({ url, data }) => {
        realConsole?.log('onMessage', url, data);
        if (data.startsWith("3:::")) {
            const eventDataObject = JSON.parse(data.slice(4));
            
            realConsole?.log('Event Data Object:', eventDataObject);
            if (eventDataObject.url.endsWith("rosterUpdate/") || eventDataObject.url.endsWith("rosterUpdate")) {
                handleRosterUpdate(eventDataObject);
            }
            if (eventDataObject.url.endsWith("conversation/conversationEnd/")) {
                handleConversationEnd(eventDataObject);
            }
            /*
            Not sure if this is needed
            if (eventDataObject.url.endsWith("controlVideoStreaming/")) {
                handleControlVideoStreaming(eventDataObject);
            }
            */
        }
    }
});

class ParticipantSpeakingStateMachine {
    constructor(participantId) {
        this.participantId = participantId;
        this.state = 'NOT_SPEAKING';
        this.samples = [];
    }

    sendSpeechStartStopEvent(participantId, isSpeechStart, timestamp) {
        window.ws?.sendJson({
            type: 'ParticipantSpeechStartStopEvent',
            participantId: participantId,
            isSpeechStart: isSpeechStart,
            timestamp: timestamp
        });
    }

    addSample(sample) {
        this.samples.push(sample);

        if (this.samples.length > 10) {
            this.samples.shift();
        }

        const lastFiveSamples = this.samples.slice(-5);
        if (lastFiveSamples.length < 5)
            return;

        const majorityOfLastFiveSamplesWereTrue = lastFiveSamples.filter(sample => sample.isSpeaking).length > 3;
        const previousState = this.state;
        const firstOfLastFiveSamplesTimestamp = lastFiveSamples[0].timestamp;
        if (majorityOfLastFiveSamplesWereTrue) {
            this.state = 'SPEAKING';
        } else {
            this.state = 'NOT_SPEAKING';
        }

        if (previousState == 'NOT_SPEAKING' && this.state == 'SPEAKING') {
            realConsole?.log('SPEAKING: adding speech start for participant', this.participantId);
            dominantSpeakerManager.addSpeechIntervalStart(firstOfLastFiveSamplesTimestamp, this.participantId);
            if (window.initialData.recordParticipantSpeechStartStopEvents)
                this.sendSpeechStartStopEvent(this.participantId, true, firstOfLastFiveSamplesTimestamp);
        } else if (previousState == 'SPEAKING' && this.state == 'NOT_SPEAKING') {
            realConsole?.log('NOT_SPEAKING: adding speech stop for participant', this.participantId);
            dominantSpeakerManager.addSpeechIntervalEnd(firstOfLastFiveSamplesTimestamp - 100, this.participantId);
            if (window.initialData.recordParticipantSpeechStartStopEvents)
                this.sendSpeechStartStopEvent(this.participantId, false, firstOfLastFiveSamplesTimestamp - 100);
        }
    }
}

class ReceiverManager {
    constructor() {
        this.receiverMap = new Map();
        this.participantSpeakingStateMachineMap = new Map();
    }

    startPollingReceivers() {
        window.ws.sendJson({
            type: 'ReceiverManagerUpdate',
            update: "startPollingReceivers"
        });
        setInterval(() => {
            this.pollReceivers();
        }, 100);
    }

    pollReceivers() {
        for (const [receiver, isActive] of this.receiverMap) {
            const contributingSources = receiver.getContributingSources();

            if (contributingSources.length > 0 && !isActive) {
                this.receiverMap.set(receiver, true);
                window.ws?.sendJson({
                    type: 'ReceiverManagerUpdate',
                    update: "setReceiverActive",
                    receiverTrackId: receiver.track?.id
                });
            }

            if (!isActive)
                continue;

            const currentTime = Date.now();
            const recentContributingSources = contributingSources.filter(contributingSource => currentTime - contributingSource.timestamp <= 50);
            const speakingParticipantIds = window.callManager?.getSpeakingParticipantIds(recentContributingSources) || [];

            for (const speakingParticipantId of speakingParticipantIds) {
                if (!this.participantSpeakingStateMachineMap.has(speakingParticipantId)) {
                    this.participantSpeakingStateMachineMap.set(speakingParticipantId, new ParticipantSpeakingStateMachine(speakingParticipantId));
                }
            }

            // Now iterate through the participantSpeakingStateMachineMap and update the isSpeaking state for each participant
            for (const [participantId, participantSpeakingStateMachine] of this.participantSpeakingStateMachineMap) {
                participantSpeakingStateMachine.addSample({
                    isSpeaking: speakingParticipantIds.has(participantId),
                    timestamp: currentTime
                });
            }
            
            /*
            {
    "rtpTimestamp": 506968569,
    "source": 414,
    "timestamp": 1759288487277
}
            */
        }
    }

    addReceiver(receiver) {
        if (!receiver || this.receiverMap.has(receiver)) return;
        realConsole?.log('ReceiverManager is adding receiver', receiver);
        window.ws?.sendJson({
            type: 'ReceiverManagerUpdate',
            update: "addReceiver",
            receiverTrackId: receiver.track?.id
        });
        this.receiverMap.set(receiver, false);
    }
}

const ws = new WebSocketClient();
window.ws = ws;
const userManager = new UserManager(ws);
window.userManager = userManager;

const chatMessageManager = new ChatMessageManager(ws);
window.chatMessageManager = chatMessageManager;

//const videoTrackManager = new VideoTrackManager(ws);
const virtualStreamToPhysicalStreamMappingManager = new VirtualStreamToPhysicalStreamMappingManager();
const dominantSpeakerManager = new DominantSpeakerManager();

const styleManager = new StyleManager();
window.styleManager = styleManager;

const receiverManager = new ReceiverManager();
window.receiverManager = receiverManager;

const processDominantSpeakerHistoryMessage = (item) => {
    realConsole?.log('processDominantSpeakerHistoryMessage', item);
    const newDominantSpeakerAudioVirtualStreamId = item.history[0];
    dominantSpeakerManager.setDominantSpeakerStreamId(newDominantSpeakerAudioVirtualStreamId);
    realConsole?.log('newDominantSpeakerParticipant', dominantSpeakerManager.getDominantSpeaker());
}

function convertTimestampAudioSentToUnixTimeMs(timestampAudioSent) {
    const fractional_seconds_since_1900 = timestampAudioSent / 10000000;
    const fractional_seconds_since_1970 = fractional_seconds_since_1900 - 2_208_988_800;
    return Math.floor(fractional_seconds_since_1970 * 1000);
}

class UtteranceIdGenerator {
    constructor(generate = () => crypto.randomUUID()) {
      this._activeIds = new Map();  // Map<speakerKey, utteranceId>
      this._generate = generate;    // Injectable for tests
    }
  
    /**
     * @param {string} speakerKey  – any stable identifier for the speaker
     * @param {boolean} isFinal    – true only on the last chunk of an utterance
     * @returns {string}           – the utteranceId to attach to this chunk
     */
    next(speakerKey = 'default', isFinal = false) {
      // Reuse or create
      let id = this._activeIds.get(speakerKey);
      if (!id) {
        id = this._generate();
        // Only keep it around if more chunks are expected
        if (!isFinal) this._activeIds.set(speakerKey, id);
      } else if (isFinal) {
        // Utterance ends: remove from the map after returning the same ID
        this._activeIds.delete(speakerKey);
      }
  
      return id;
    }
  
    /** Optional: free all state (e.g., when a call ends) */
    dispose() {
      this._activeIds.clear();
    }
}

const utteranceIdGenerator = new UtteranceIdGenerator();

window.captureDominantSpeakerViaCaptions = false;

const processClosedCaptionData = (item) => {
    realConsole?.log('processClosedCaptionData', item);

    // If we're collecting per participant audio, we actually need the caption data because it's the most accurate
    // way to estimate when someone started speaking.
    if (window.initialData.sendPerParticipantAudio && window.captureDominantSpeakerViaCaptions)
    {
        const timeStampAudioSentUnixMs = convertTimestampAudioSentToUnixTimeMs(item.timestampAudioSent);
        dominantSpeakerManager.addCaptionAudioTime(timeStampAudioSentUnixMs, item.userId);
    }

    // If we don't need the captions, we can leave.
    if (!window.initialData.collectCaptions)
    {
        return;
    }

    if (!window.ws) {
        return;
    }

    const itemConverted = {
        deviceId: item.userId,
        captionId: utteranceIdGenerator.next(item.userId, item.isFinal),
        text: item.text,
        audioTimestamp: item.timestampAudioSent,
        isFinal: item.isFinal
    };
    
    window.ws.sendClosedCaptionUpdate(itemConverted);
}

const decodeMainChannelData = (data) => {
    const decodedData = new Uint8Array(data);
    for (let i = 0; i < decodedData.length; i++) {
        if (decodedData[i] === 91 || decodedData[i] === 123) { // ASCII code for '[' or '{'
            const candidateJsonString = new TextDecoder().decode(decodedData.slice(i));
            try {
                return JSON.parse(candidateJsonString);
            }
            catch(e) {
                if (e instanceof SyntaxError) {
                    // If JSON parsing fails, continue looking for the next '[' or '{' character
                    // as binary data may contain bytes that coincidentally match these character codes
                    continue;
                }
                realConsole?.error('Failed to parse main channel data:', e);
                return;            
            }        
        }
    }
}

const handleMainChannelEvent = (event) => {
    try {
        const parsedData = decodeMainChannelData(event.data);
        if (!parsedData) {
            realConsole?.error('handleMainChannelEvent: Failed to parse main channel data, returning, data:', event.data);
            return;
        }
        realConsole?.log('handleMainChannelEvent parsedData', parsedData);
        // When you see this parsedData [{"history":[1053,2331],"type":"dsh"}]
        // it corresponds to active speaker
        if (Array.isArray(parsedData)) {
            for (const item of parsedData) {
                // This is a dominant speaker history message
                if (item.type === 'dsh') {
                    processDominantSpeakerHistoryMessage(item);
                }
            }
        }
        else
        {
            if (parsedData.recognitionResults) {
                for(const item of parsedData.recognitionResults) {
                    processClosedCaptionData(item);
                }
            }
        }
    } catch (e) {
        realConsole?.error('handleMainChannelEvent: Failed to parse main channel data:', e);
    }
}

const processSourceRequest = (item) => {
    const sourceId = item?.controlVideoStreaming?.controlInfo?.sourceId;
    const streamMsid = item?.controlVideoStreaming?.controlInfo?.streamMsid;

    if (!sourceId || !streamMsid) {
        return;
    }

    virtualStreamToPhysicalStreamMappingManager.upsertPhysicalClientStreamIdToVirtualStreamIdMapping(streamMsid.toString(), sourceId.toString());
}

const handleMainChannelSend = (data) => {

    try {
        const parsedData = decodeMainChannelData(data);
        if (!parsedData) {
            realConsole?.error('handleMainChannelSend: Failed to parse main channel data, returning, data:', data);
            return;
        }
        realConsole?.log('handleMainChannelSend parsedData', parsedData);  
        // if it is an array
        if (Array.isArray(parsedData)) {
            for (const item of parsedData) {
                // This is a source request. It means the teams client is asking for the server to start serving a source from one of the streams
                // that the server provides to the client
                if (item.type === 'sr') {
                    processSourceRequest(item);
                }
            }
        }
    } catch (e) {
        realConsole?.error('handleMainChannelSend: Failed to parse main channel data:', e);
    }
}

const handleVideoTrack = async (event) => {  
    try {
      // Create processor to get raw frames
      const processor = new MediaStreamTrackProcessor({ track: event.track });
      const generator = new MediaStreamTrackGenerator({ kind: 'video' });
      
      // Add track ended listener
      event.track.addEventListener('ended', () => {
          console.log('Video track ended:', event.track.id);
          //videoTrackManager.deleteVideoTrack(event.track);
      });
      
      // Get readable stream of video frames
      const readable = processor.readable;
      const writable = generator.writable;
  
      const firstStreamId = event.streams[0]?.id;

      console.log('firstStreamId', firstStreamId);
  
      // Check if of the users who are in the meeting and screensharers
      // if any of them have an associated device output with the first stream ID of this video track
      /*
      const isScreenShare = userManager
          .getCurrentUsersInMeetingWhoAreScreenSharing()
          .some(user => firstStreamId && userManager.getDeviceOutput(user.deviceId, DEVICE_OUTPUT_TYPE.VIDEO).streamId === firstStreamId);
      if (firstStreamId) {
          videoTrackManager.upsertVideoTrack(event.track, firstStreamId, isScreenShare);
      }
          */
  
      // Add frame rate control variables
      const targetFPS = 24;
      const frameInterval = 1000 / targetFPS; // milliseconds between frames
      let lastFrameTime = 0;
  
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
  
                  const currentTime = performance.now();
  
                  // Add SSRC logging 
                  // 
                  /*
                  if (event.track.getSettings) {
                      //console.log('Track settings:', event.track.getSettings());
                  }
                  //console.log('Track ID:', event.track.id);
                 
                  if (event.streams && event.streams[0]) {
                      //console.log('Stream ID:', event.streams[0].id);
                      event.streams[0].getTracks().forEach(track => {
                          if (track.getStats) {
                              track.getStats().then(stats => {
                                  stats.forEach(report => {
                                      if (report.type === 'outbound-rtp' || report.type === 'inbound-rtp') {
                                          console.log('RTP Stats (including SSRC):', report);
                                      }
                                  });
                              });
                          }
                      });
                  }*/
  
                  /*
                  if (Math.random() < 0.00025) {
                    //const participant = virtualStreamToPhysicalStreamMappingManager.physicalServerStreamIdToParticipant(firstStreamId);
                    //realConsole?.log('videoframe from stream id', firstStreamId, ' corresponding to participant', participant);
                    //realConsole?.log('frame', frame);
                    //realConsole?.log('handleVideoTrack, randomsample', event);
                  }
                    */
                 // if (Math.random() < 0.02)
                   //realConsole?.log('firstStreamId', firstStreamId, 'streamIdToSend', virtualStreamToPhysicalStreamMappingManager.getVideoStreamIdToSend());
                  
                  if (firstStreamId && firstStreamId === virtualStreamToPhysicalStreamMappingManager.getVideoStreamIdToSend()) {
                      // Check if enough time has passed since the last frame
                      if (currentTime - lastFrameTime >= frameInterval) {
                          // Copy the frame to get access to raw data
                          const rawFrame = new VideoFrame(frame, {
                              format: 'I420'
                          });
  
                          // Get the raw data from the frame
                          const data = new Uint8Array(rawFrame.allocationSize());
                          rawFrame.copyTo(data);
  
                          /*
                          const currentFormat = {
                              width: frame.displayWidth,
                              height: frame.displayHeight,
                              dataSize: data.length,
                              format: rawFrame.format,
                              duration: frame.duration,
                              colorSpace: frame.colorSpace,
                              codedWidth: frame.codedWidth,
                              codedHeight: frame.codedHeight
                          };
                          */
                          // Get current time in microseconds (multiply milliseconds by 1000)
                          const currentTimeMicros = BigInt(Math.floor(currentTime * 1000));
                          ws.sendVideo(currentTimeMicros, firstStreamId, frame.displayWidth, frame.displayHeight, data);
  
                          rawFrame.close();
                          lastFrameTime = currentTime;
                      }
                  }
                  
                  // Always enqueue the frame for the video element
                  controller.enqueue(frame);
              } catch (error) {
                  realConsole?.error('Error processing frame:', error);
                  frame.close();
              }
          },
          flush() {
              realConsole?.log('Transform stream flush called');
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
                      realConsole?.error('Pipeline error:', error);
                  }
              });
      } catch (error) {
          realConsole?.error('Stream pipeline error:', error);
          abortController.abort();
      }
  
    } catch (error) {
        realConsole?.error('Error setting up video interceptor:', error);
    }
  };

  const globalAudioQueueIntervalsSet = new Set();

  const handleAudioTrack = async (event) => {
    let lastAudioFormat = null;  // Track last seen format
    const audioDataQueue = [];
    const ACTIVE_SPEAKER_LATENCY_MS = 2000;
    let trackIsNonSilent = false;
    let handleAudioTrackDebugInfo = {
        framesWithoutDominantSpeaker: 0,
        framesWithDominantSpeaker: 0,
        totalFrames: 0,
    };
    let timeSinceLastDebugInfoSend = 0;

    window.receiverManager.addReceiver(event.receiver);
    
    // Start continuous background processing of the audio queue
    const processAudioQueue = () => {
        while (audioDataQueue.length > 0 && 
            Date.now() - audioDataQueue[0].audioArrivalTime >= ACTIVE_SPEAKER_LATENCY_MS) {
            const { audioData, audioArrivalTime } = audioDataQueue.shift();

            // Get the dominant speaker and assume that's who the participant speaking is
            const dominantSpeakerId = dominantSpeakerManager.getSpeakerIdForTimestampMsUsingSpeechIntervals(audioArrivalTime);

            // Send audio data through websocket
            handleAudioTrackDebugInfo.totalFrames++;
            if (dominantSpeakerId) {
                ws.sendPerParticipantAudio(dominantSpeakerId, audioData);
                handleAudioTrackDebugInfo.framesWithDominantSpeaker++;
            }
            else
            {
                handleAudioTrackDebugInfo.framesWithoutDominantSpeaker++;
            }
        }

        if (Date.now() - timeSinceLastDebugInfoSend >= 10000)  {
            timeSinceLastDebugInfoSend = Date.now();
            ws.sendJson({
                type: 'HandleAudioTrackDebugInfo',
                trackId: event.track?.id,
                debugInfo: handleAudioTrackDebugInfo
            });
            handleAudioTrackDebugInfo = {
                framesWithoutDominantSpeaker: 0,
                framesWithDominantSpeaker: 0,
                totalFrames: 0,
            };
        }
    };

    // Set up background processing every 100ms
    const queueProcessingInterval = setInterval(processAudioQueue, 100);
    globalAudioQueueIntervalsSet.add(queueProcessingInterval);
    if (globalAudioQueueIntervalsSet.size > 1) {
        window.ws?.sendJson({
            type: 'MultipleAudioQueuesDetected',
            trackId: event.track?.id,
        });
    }
    
    // Clean up interval when track ends
    event.track.addEventListener('ended', () => {
        clearInterval(queueProcessingInterval);
        console.log('Audio track ended, cleared queue processing interval');
        window.ws?.sendJson({
            type: 'AudioTrackEnded',
            trackId: event.track?.id,
        });
    });
    
    try {
      // Create processor to get raw frames
      const processor = new MediaStreamTrackProcessor({ track: event.track });
      const generator = new MediaStreamTrackGenerator({ kind: 'audio' });
      
      // Get readable stream of audio frames
      const readable = processor.readable;
      const writable = generator.writable;
  
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
  
                  // If the audioData buffer is all zeros, we still want to send it. It's only one mixed audio stream.
                  // It seems to help with the transcription.
                  //if (audioData.every(value => value === 0)) {
                  //    return;
                  //}

                  if (!trackIsNonSilent && audioData.some(value => value !== 0)) {
                    trackIsNonSilent = true;
                    window.ws?.sendJson({
                        type: 'WebRTCTrackIsNonSilent',
                        trackId: event.track?.id,
                    });
                  }

                  // Don't bother sending unless we've gotten some non-silent audio data in this track.
                  if (!trackIsNonSilent) {
                    return;
                  }

                  // If we have multiple audio queues, we hit multiple audioTracks, so we're in an irregular state. Filter out non-zero audio data.
                  if (globalAudioQueueIntervalsSet.size > 1 && !audioData.some(value => value !== 0)) {
                    return;
                  }

                  // Add to queue with timestamp - the background thread will process it
                  audioDataQueue.push({
                    audioArrivalTime: Date.now(),
                    audioData: audioData
                  });

                  // Pass through the original frame
                  controller.enqueue(frame);
              } catch (error) {
                  console.error('Error processing frame:', error);
                  frame.close();
              }
          },
          flush() {
              console.log('Transform stream flush called');
              // Clear the interval when the stream ends
              clearInterval(queueProcessingInterval);
              window.ws?.sendJson({
                type: 'AudioQueueFlush',
                trackId: event.track?.id,
            });
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
                  // Clear the interval on error
                  clearInterval(queueProcessingInterval);
              });
      } catch (error) {
          console.error('Stream pipeline error:', error);
          abortController.abort();
          // Clear the interval on error
          clearInterval(queueProcessingInterval);
          window.ws?.sendJson({
            type: 'AudioQueueError',
            trackId: event.track?.id,
          });
      }
  
    } catch (error) {
        console.error('Error setting up audio interceptor:', error);
        // Clear the interval on error
        clearInterval(queueProcessingInterval);
        window.ws?.sendJson({
            type: 'AudioQueueError',
            trackId: event.track?.id,
        });
    }
  };
  

// LOOK FOR https://api.flightproxy.skype.com/api/v2/cpconv

// LOOK FOR https://teams.live.com/api/chatsvc/consumer/v1/threads?view=msnp24Equivalent&threadIds=19%3Ameeting_Y2U4ZDk5NzgtOWQwYS00YzNjLTg2ODktYmU5MmY2MGEyNzJj%40thread.v2
new RTCInterceptor({
    onPeerConnectionCreate: (peerConnection) => {
        if (!window.__attendeePeerConnections) {
            window.__attendeePeerConnections = new Set();
        }
        window.__attendeePeerConnections.add(peerConnection);
        realConsole?.log('New RTCPeerConnection created:', peerConnection);
        peerConnection.addEventListener('connectionstatechange', () => {
            if (peerConnection.connectionState === 'closed' || peerConnection.connectionState === 'failed') {
                window.__attendeePeerConnections?.delete(peerConnection);
            }
        });
        peerConnection.addEventListener('datachannel', (event) => {
            realConsole?.log('datachannel', event);
            realConsole?.log('datachannel label', event.channel.label);

            if (event.channel.label === "collections") {               
                event.channel.addEventListener("message", (messageEvent) => {
                    console.log('RAWcollectionsevent', messageEvent);
                    handleCollectionEvent(messageEvent);
                });
            }
        });

        peerConnection.addEventListener('track', (event) => {
            console.log('New track:', {
                trackId: event.track?.id,
                trackKind: event.track?.kind,
                streams: event.streams,
            });
            window.ws?.sendJson({
                type: 'WebRTCTrackStarted',
                trackId: event.track?.id,
                trackKind: event.track?.kind,
                streams: event.streams?.map(stream => stream?.id),
            });
            // We need to capture every audio track in the meeting,
            // but we don't need to do anything with the video tracks
            if (event.track?.kind === 'audio') {
                window.styleManager.addAudioTrack(event.track);
                if (window.initialData.sendPerParticipantAudio) {
                    handleAudioTrack(event);
                }
                else if (window.initialData.recordParticipantSpeechStartStopEvents) {
                    // If we are not recording per participant audio but we need speech start events, then we only need to track the receiver.
                    window.receiverManager?.addReceiver(event.receiver);
                }
            }
            if (event.track?.kind === 'video') {
                window.styleManager.addVideoTrack(event);
                window.ws?.runVideoProbe?.();
            }
        });

        /*
        We are no longer setting up per-frame MediaStreamTrackProcessor's because it taxes the CPU too much
        For now, we are just using the ScreenAndAudioRecorder to record the video stream
        but we're keeping this code around for reference
        peerConnection.addEventListener('track', (event) => {
            // Log the track and its associated streams

            if (event.track.kind === 'audio') {
                realConsole?.log('got audio track');
                realConsole?.log(event);
                try {
                    handleAudioTrack(event);
                } catch (e) {
                    realConsole?.log('Error handling audio track:', e);
                }
            }
            if (event.track.kind === 'video') {
                realConsole?.log('got video track');
                realConsole?.log(event);
                try {
                    handleVideoTrack(event);
                } catch (e) {
                    realConsole?.log('Error handling video track:', e);
                }
            }
        });
        */

        peerConnection.addEventListener('connectionstatechange', (event) => {
            realConsole?.log('connectionstatechange', event);
        });
        


        // This is called when the browser detects that the SDP has changed
        peerConnection.addEventListener('negotiationneeded', (event) => {
            realConsole?.log('negotiationneeded', event);
        });

        peerConnection.addEventListener('onnegotiationneeded', (event) => {
            realConsole?.log('onnegotiationneeded', event);
        });

        // Log the signaling state changes
        peerConnection.addEventListener('signalingstatechange', () => {
            console.log('Signaling State:', peerConnection.signalingState);
        });

        // Log the SDP being exchanged
        const originalSetLocalDescription = peerConnection.setLocalDescription;
        peerConnection.setLocalDescription = function(description) {
            realConsole?.log('Local SDP:', description);
            return originalSetLocalDescription.apply(this, arguments);
        };

        const originalSetRemoteDescription = peerConnection.setRemoteDescription;
        peerConnection.setRemoteDescription = function(description) {
            realConsole?.log('Remote SDP:', description);
            return originalSetRemoteDescription.apply(this, arguments);
        };

        // Log ICE candidates
        peerConnection.addEventListener('icecandidate', (event) => {
            if (event.candidate) {
                //console.log('ICE Candidate:', event.candidate);
            }
        });
    },
    onDataChannelCreate: (dataChannel, peerConnection) => {
        realConsole?.log('New DataChannel created:', dataChannel);
        realConsole?.log('On PeerConnection:', peerConnection);
        realConsole?.log('Channel label:', dataChannel.label);
        realConsole?.log('Channel keys:', typeof dataChannel);

        //if (dataChannel.label === 'collections') {
          //  dataChannel.addEventListener("message", (event) => {
         //       console.log('collectionsevent', event)
        //    });
        //}


      if (dataChannel.label === 'main-channel') {
        dataChannel.addEventListener("message", (mainChannelEvent) => {
            handleMainChannelEvent(mainChannelEvent);
        });
      }
    },
    onDataChannelSend: ({channel, data, peerConnection}) => {
        if (channel.label === 'main-channel') {
            handleMainChannelSend(data);
        }
        
        
        /*
        realConsole?.log('DataChannel send intercepted:', {
            channelLabel: channel.label,
            data: data,
            readyState: channel.readyState
        });*/

        // It looks like it sends a payload like this:
        /*
            [{"type":"sr","controlVideoStreaming":{"sequenceNumber":11,"controlInfo":{"sourceId":1267,"streamMsid":1694,"fmtParams":[{"max-fs":920,"max-mbps":33750,"max-fps":3000,"profile-level-id":"64001f"}]}}}]

            The streamMsid corresponds to the streamId in the streamIdToSSRCMapping object. We can use it to get the actual stream's id by putting it through the mapping and getting the ssrc.
            The sourceId corresponds to sourceId of the participant that you get from the roster update event.
            Annoyingly complicated, but it seems to work.



        */
    }
});

function addClickRipple() {
    document.addEventListener('click', function(e) {
      const ripple = document.createElement('div');
      
      // Apply styles directly to the element
      ripple.style.position = 'fixed';
      ripple.style.borderRadius = '50%';
      ripple.style.width = '20px';
      ripple.style.height = '20px';
      ripple.style.marginLeft = '-10px';
      ripple.style.marginTop = '-10px';
      ripple.style.background = 'red';
      ripple.style.opacity = '0';
      ripple.style.pointerEvents = 'none';
      ripple.style.transform = 'scale(0)';
      ripple.style.transition = 'transform 0.3s, opacity 0.3s';
      ripple.style.zIndex = '9999999';
      
      ripple.style.left = e.pageX + 'px';
      ripple.style.top = e.pageY + 'px';
      document.body.appendChild(ripple);
  
      // Force reflow so CSS transition will play
      getComputedStyle(ripple).transform;
      
      // Animate
      ripple.style.transform = 'scale(3)';
      ripple.style.opacity = '0.7';
  
      // Remove after animation
      setTimeout(() => {
        ripple.remove();
      }, 300);
    }, true);
}

if (window.initialData.addClickRipple) {
    addClickRipple();
}



async function turnOnCamera() {
    // Click camera button to turn it on
    let cameraButton = null;
    const numAttempts = 30;
    for (let i = 0; i < numAttempts; i++) {
        cameraButton = document.querySelector('button[aria-label="Turn camera on"]') || document.querySelector('div[aria-label="Turn camera on"]');
        if (cameraButton) {
            break;
        }
        window.ws?.sendJson({
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
        window.ws?.sendJson({
            type: 'Error',
            message: 'Camera button not found in turnOnCamera'
        });
    }
}

function turnOnMic() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Unmute mic"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    }
}

function turnOffMic() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Mute mic"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    }
}

function turnOnMicAndCamera() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Unmute mic"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }

    // Click camera button to turn it on
    const cameraButton = document.querySelector('button[aria-label="Turn camera on"]');
    if (cameraButton) {
        console.log("Clicking the camera button to turn it on");
        cameraButton.click();
    } else {
        console.log("Camera button not found");
    }
}

function turnOffMicAndCamera() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector('button[aria-label="Mute mic"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click camera button to turn it off
    const cameraButton = document.querySelector('button[aria-label="Turn camera off"]');
    if (cameraButton) {
        console.log("Clicking the camera button to turn it off");
        cameraButton.click();
    } else {
        console.log("Camera off button not found");
    }
}

function turnOffCamera() {
    // Click camera button to turn it off
    const cameraButton = document.querySelector('button[aria-label="Turn camera off"]');
    if (cameraButton) {
        console.log("Clicking the camera button to turn it off");
        cameraButton.click();
    } else {
        console.log("Camera off button not found");
    }
}

const turnOnMicArialLabel = "Unmute mic"
const turnOnScreenshareButtonId = "screenshare-button"
const turnOnScreenshareButtonAlternateId = "share-button"
const turnOffMicArialLabel = "Turn off microphone"
const turnOffScreenshareAriaLabel = "Stop sharing"

function turnOnMicAndScreenshare() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector(`button[aria-label="${turnOnMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
        window.ws.sendJson({
            turnOnMicAndScreenshareError: "Microphone button not found in turnOnMicAndScreenshare"
        });
    }

    // Click screenshare button to turn it on
    const screenshareButton = document.querySelector(`button[id="${turnOnScreenshareButtonId}"]`) || document.querySelector(`button[id="${turnOnScreenshareButtonAlternateId}"]`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it on");
        screenshareButton.click();
    } else {
        console.log("Screenshare button not found");
        window.ws.sendJson({
            turnOnMicAndScreenshareError: "Screenshare button not found in turnOnMicAndScreenshare"
        });
    }
}

function turnOffMicAndScreenshare() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector(`button[aria-label="${turnOffMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click screenshare button to turn it off
    const screenshareButton = document.querySelector(`button[aria-label="${turnOffScreenshareAriaLabel}"]`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it off");
        screenshareButton.click();
    } else {
        console.log("Screenshare off button not found");
    }
}


function turnOnScreenshare() {
    // Click screenshare button to turn it on
    const screenshareButton = document.querySelector(`button[id="${turnOnScreenshareButtonId}"]`) || document.querySelector(`button[id="${turnOnScreenshareButtonAlternateId}"]`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it on");
        screenshareButton.click();
    } else {
        console.log("Screenshare button not found");
        window.ws.sendJson({
            turnOnMicAndScreenshareError: "Screenshare button not found in turnOnMicAndScreenshare"
        });
    }
}

function turnOffScreenshare() {
    // Click screenshare button to turn it off
    const screenshareButton = document.querySelector(`button[aria-label="${turnOffScreenshareAriaLabel}"]`);
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
    turnOffWebcam: turnOffCamera,
    turnOnScreenshare: turnOnScreenshare,
    turnOffScreenshare: turnOffScreenshare,
    turnOnMic: turnOnMic,
    turnOffMic: turnOffMic,
});

window.botOutputManager = botOutputManager;

(function () {
    const _bind = Function.prototype.bind;
    Function.prototype.bind = function (thisArg, ...args) {
        if (this.name === 'onMessageReceived') {
            const bound = _bind.apply(this, [thisArg, ...args]);
            return function (...callArgs) {
                const eventData = callArgs[0];
                if (eventData?.data?.chatServiceBatchEvent)
                {
                    const batchEvents = eventData.data.chatServiceBatchEvent;
                    if (Array.isArray(batchEvents))
                    {
                        for (const event of batchEvents) 
                        {
                            if (event?.message)
                            {
                                realConsole?.log('chatMessage', event.message);
                                window.chatMessageManager?.handleChatMessage(event.message);
                            }
                        }
                    }
                }
                return bound.apply(this, callArgs);
            };
        }
        return _bind.apply(this, [thisArg, ...args]);
    };
})();

class CallManager {
    constructor() {
        this.activeCall = null;
        this.closedCaptionLanguageInterval = null;
        this.closedCaptionLanguage = null;
    }

    setActiveCall() {
        if (this.activeCall) {
            return;
        }

        if (window.callingDebug?.observableCall) {
            this.activeCall = window.callingDebug.observableCall;
        }

        if (window.msteamscalling?.deref)
        {
            const microsoftCalling = window.msteamscalling.deref();
            if (microsoftCalling?.callingService?.getActiveCall) {
                const call = microsoftCalling.callingService.getActiveCall();
                if (call) {
                    this.activeCall = call;
                }
            }
        }
    }

    getCallId() {
        this.setActiveCall();
        if (!this.activeCall) {
            return;
        }

        return this.activeCall._callId;
    }

    getCurrentUserId() {
        this.setActiveCall();
        if (!this.activeCall) {
            return;
        }

        return this.activeCall.callerMri;
        // We're using callerMri because it includes the 8: prefix. If callerMri stops working, we can easily use the thing below.
        // return this.activeCall.currentUserSkypeIdentity?.id;
    }


    getSpeakingParticipantIds(contributingSources) {
        this.setActiveCall();
        if (!this.activeCall) {
            return [];
        }
        if (!this.activeCall.participants) {
            return [];
        }

        const speakingParticipantIds = new Set();

        this.activeCall.participants.forEach(participant => {
            if (contributingSources.some(contributingSource => participant.hasAudioSource(contributingSource.source)) && participant.id)
                speakingParticipantIds.add(participant.id);
        });

        return speakingParticipantIds;
    }

    syncParticipants() {
        this.setActiveCall();
        if (!this.activeCall) {
            return;
        }

        const participantsRaw = this.activeCall.participants;
        const participants = participantsRaw.map(participant => {
            return {
                id: participant.id,
                displayName: participant.displayName,
                endpoints: participant.endpoints,
                meetingRole: participant.meetingRole
            };
        }).filter(participant => participant.displayName);

        for (const participant of participants) {
            const endpoints = (participant?.endpoints?.endpointDetails || []).map(endpoint => {
                if (!endpoint.endpointId) {
                    return null;
                }

                if (!endpoint.mediaStreams) {
                    return null;
                }

                return [
                    endpoint.endpointId,
                    {
                        call: {
                            mediaStreams: endpoint.mediaStreams
                        }
                    }
                ]
            }).filter(endpoint => endpoint);

            // Transform this funny format of a participant into Teams "standard" format
            const participantConverted = {
                details: {id: participant.id, displayName: participant.displayName},
                meetingRole: participant.meetingRole,
                state: "active",
                endpoints: Object.fromEntries(endpoints),
                callId: this.getCallId()
            };
            window.userManager.singleUserSynced(participantConverted);
            syncVirtualStreamsFromParticipant(participantConverted);
        }
    }

    enableClosedCaptions() {
        this.setActiveCall();
        if (this.activeCall) {
            this.activeCall.startClosedCaption();
            return true;
        }
        return false;
    }

    setClosedCaptionsLanguage(language) {
        this.setActiveCall();
        if (this.activeCall) {
            this.closedCaptionLanguage = language;
            this.activeCall.setClosedCaptionsLanguage(this.closedCaptionLanguage);
            // Unfortunately, this is needed for improved reliability.
            // It seems like when the host joins at the same time as the bot, they reset the cc language to the default.
            setTimeout(() => {
                if (this.activeCall) {
                    this.activeCall.setClosedCaptionsLanguage(this.closedCaptionLanguage);
                }
            }, 1000);         
            setTimeout(() => {
                if (this.activeCall) {
                    this.activeCall.setClosedCaptionsLanguage(this.closedCaptionLanguage);
                }
            }, 3000);
            setTimeout(() => {
                if (this.activeCall) {
                    this.activeCall.setClosedCaptionsLanguage(this.closedCaptionLanguage);
                }
            }, 5000);
            setTimeout(() => {
                if (this.activeCall) {
                    this.activeCall.setClosedCaptionsLanguage(this.closedCaptionLanguage);
                    // Set an interval that runs every 60 seconds and makes sure the current closed caption language is equal to the language
                    // This is for debugging purposes

                    // Only do it if the interval is not already set
                    if (this.closedCaptionLanguageInterval)
                        return;
                    this.closedCaptionLanguageInterval = setInterval(() => {
                        if (this.activeCall && this.activeCall.getClosedCaptionsLanguage) {
                            if (this.activeCall.getClosedCaptionsLanguage() !== this.closedCaptionLanguage) {
                                window.ws?.sendJson({
                                    type: "closedCaptionsLanguageMismatch",
                                    desiredLanguage: this.closedCaptionLanguage,
                                    currentLanguage: this.activeCall.getClosedCaptionsLanguage()
                                });
                            }
                        }
                    }, 60000);
                }
            }, 10000);
            return true;
        }
        return false;
    }
}

const callManager = new CallManager();
window.callManager = callManager;
